/* Host stub for NimBLE host/ble_gatt.h. */
#ifndef STUB_BLE_GATT_H
#define STUB_BLE_GATT_H
#include <stdint.h>
#include <stddef.h>
#include "ble_uuid.h"

struct os_mbuf;
struct ble_gatt_access_ctxt {
    int op;
    struct os_mbuf *om;
};

#define BLE_GATT_ACCESS_OP_READ_CHR 1
#define BLE_GATT_ACCESS_OP_WRITE_CHR 2
#define BLE_GATT_CHR_F_READ (1 << 1)
#define BLE_GATT_CHR_F_WRITE (1 << 3)
#define BLE_GATT_CHR_F_NOTIFY (1 << 4)
#define BLE_GATT_SVC_TYPE_PRIMARY 1

#define BLE_ATT_ERR_UNLIKELY 1
#define BLE_ATT_ERR_READ_NOT_PERMITTED 2
#define BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN 13
#define BLE_ATT_ERR_INSUFFICIENT_RES 17

struct ble_gatt_chr_def {
    const struct ble_uuid_t *uuid;
    int (*access_cb)(uint16_t conn_handle, uint16_t attr_handle,
                     struct ble_gatt_access_ctxt *ctxt, void *arg);
    void *arg;
    uint8_t flags;
    uint16_t *val_handle;
};

struct ble_gatt_svc_def {
    uint8_t type;
    const struct ble_uuid_t *uuid;
    const struct ble_gatt_chr_def *characteristics;
};

int ble_gatts_count_cfg(const struct ble_gatt_svc_def *svcs);
int ble_gatts_add_svcs(const struct ble_gatt_svc_def *svcs);
int ble_hs_mbuf_to_flat(struct os_mbuf *om, void *dst, uint16_t max_len, uint16_t *out_len);
struct os_mbuf *ble_hs_mbuf_from_flat(const void *data, uint16_t len);
void os_mbuf_free_chain(struct os_mbuf *om);
int os_mbuf_append(struct os_mbuf *om, const void *data, uint16_t len);
#endif