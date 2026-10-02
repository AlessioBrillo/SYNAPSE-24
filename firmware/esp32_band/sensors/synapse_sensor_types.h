/**
 * @file synapse_sensor_types.h
 * @brief Shared sensor data types (single source of truth)
 * Avoids duplicate definitions across synapse_sensors.h, gps_max_m10s.h, temp_tmp117.h
 */

#ifndef SYNAPSE_SENSOR_TYPES_H
#define SYNAPSE_SENSOR_TYPES_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef GPS_DATA_T_DEFINED
#define GPS_DATA_T_DEFINED
typedef struct {
    bool valid;
    bool has_fix;
    uint8_t fix_type;
    double latitude;
    double longitude;
    float altitude;
    float speed_mps;
    float heading_deg;
    float hdop;
    uint8_t num_satellites;
    int64_t timestamp_us;
    int64_t local_timestamp_us;
} gps_data_t;
#endif

#ifndef TEMP_DATA_T_DEFINED
#define TEMP_DATA_T_DEFINED
typedef struct {
    float temperature_c;
    bool valid;
    int64_t timestamp_us;
} temp_data_t;
#endif

#ifndef GPS_CFG_T_DEFINED
#define GPS_CFG_T_DEFINED
typedef struct {
    int uart_port;
    int tx_gpio;
    int rx_gpio;
    int baudrate;
    bool use_ubx;
    int uart_rx_buffer_size;
} gps_max_m10s_config_t;
#endif

#ifndef TEMP_CFG_T_DEFINED
#define TEMP_CFG_T_DEFINED
typedef struct {
    int i2c_port;
    int i2c_addr;
    int gpio_alert;
    uint16_t conversion_cycle_ms;
    bool continuous_mode;
} temp_tmp117_config_t;
#endif

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_SENSOR_TYPES_H
