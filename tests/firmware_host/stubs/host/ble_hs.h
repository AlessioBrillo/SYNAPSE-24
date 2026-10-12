/* Host stub for NimBLE host/ble_hs.h (see esp_idf_stubs.h). */
#ifndef STUB_BLE_HS_H
#define STUB_BLE_HS_H
#include <stdint.h>
#include <stddef.h>
#define BLE_HS_CONN_HANDLE_NONE 0xFFFF
struct os_mbuf;
int ble_gatts_notify_custom(uint16_t conn_handle, uint16_t attr_handle, struct os_mbuf *om);
#endif