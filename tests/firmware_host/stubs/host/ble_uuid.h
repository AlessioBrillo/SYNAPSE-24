/* Host stub for NimBLE host/ble_uuid.h. */
#ifndef STUB_BLE_UUID_H
#define STUB_BLE_UUID_H
#include <stdint.h>
#define BLE_UUID_TYPE_128 16
struct ble_uuid_t {
    uint8_t type;
};
/* NimBLE exposes the 16 raw bytes as .u on the generic ble_uuid_t. */
typedef struct {
    uint8_t type;
    union {
        uint8_t value[16];
        struct ble_uuid_t u;
    };
} ble_uuid128_t;
#define BLE_UUID128_INIT(b15,b14,b13,b12,b11,b10,b9,b8,b7,b6,b5,b4,b3,b2,b1,b0) \
    { BLE_UUID_TYPE_128, { { b0,b1,b2,b3,b4,b5,b6,b7,b8,b9,b10,b11,b12,b13,b14,b15 } } }
#endif