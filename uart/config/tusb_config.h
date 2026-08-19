#pragma once

#include <tusb_option.h>

#define CFG_TUSB_RHPORT0_MODE OPT_MODE_DEVICE

//#define CFG_TUSB_DEBUG      2

//#define CFG_TUD_ENABLED     1

#ifdef ENABLE_DEBUG_STDIO
#define CFG_TUD_CDC             4
#else
#define CFG_TUD_CDC             3
#endif
#define CFG_TUD_CDC_RX_BUFSIZE  256
#define CFG_TUD_CDC_TX_BUFSIZE  256
