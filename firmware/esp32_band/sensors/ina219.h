/**
 * @file ina219.h
 * @brief Synapse Band v1 - INA219 current/power monitor (I2C).
 *
 * Measures bus voltage, shunt current and power on the pod's supply rail.
 * This is the sensor behind Architecture.md §55-62's "energy budget as a
 * functional requirement": a bench PSU reading is a one-off calibration,
 * while the 24-hour feasibility claim needs the *actual* per-session
 * consumption logged with the recording (Principle 8).
 *
 * Breakout modules normally ship with a 0.1 Ω shunt resistor and address
 * 0x40 (A0/A1 grounded). Both are configurable because clone modules vary.
 */

#ifndef INA219_H
#define INA219_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#ifdef __cplusplus
extern "C" {
#endif

// INA219 register addresses (16-bit values, big-endian on the wire).
#define INA219_REG_CONFIG      0x00
#define INA219_REG_SHUNT_V     0x01
#define INA219_REG_BUS_V       0x02
#define INA219_REG_POWER       0x03
#define INA219_REG_CURRENT     0x04
#define INA219_REG_CALIBRATION 0x05

// Configuration register fields (16-bit, same as written).
//   [15] RST      [13] BRNG (0=16V, 1=32V) [12-11] PG (gain)
//   [10-7] BADC (bus ADC) [6-3] SADC (shunt ADC) [2-0] MODE
#define INA219_CFG_RESET           (1u << 15)
#define INA219_CFG_BUS_RANGE_32V   (1u << 13)
#define INA219_CFG_PGA_DIV8        (3u << 11)   /**< ±320 mV shunt range     */
#define INA219_CFG_PGA_DIV4        (2u << 11)   /**< ±160 mV                 */
#define INA219_CFG_PGA_DIV2        (1u << 11)   /**< ±80 mV                  */
#define INA219_CFG_PGA_DIV1        (0u << 11)   /**< ±40 mV                  */
#define INA219_CFG_ADC_12BIT_1S    (0x3u << 7)  /**< bus ADC: 12-bit, 1 sample */
#define INA219_CFG_ADC_12BIT_8S    (0xCu << 7)
#define INA219_CFG_SADC_12BIT_8S   (0xCu << 3)  /**< shunt ADC: 12-bit, 8 samples */
#define INA219_CFG_MODE_CONT_ALL   0x07u        /**< continuous shunt+bus   */

// Fixed-point readout scalings from the datasheet.
#define INA219_BUS_V_LSB_MV    4.0f   /**< 4 mV per LSB, 3 LSBs (right-aligned bits) */
#define INA219_SHUNT_V_LSB_UV  10.0f  /**< 10 uV per LSB                          */
#define INA219_CURRENT_LSB_MA  10.0f  /**< default; recomputed from the shunt     */

typedef struct {
    uint8_t i2c_port;
    uint8_t i2c_addr;        /**< 0x40 with A0/A1 grounded */
    float shunt_ohm;         /**< 0.1 on standard breakouts */
    float max_expected_a;    /**< selects the PGA and calibration scaling */
} ina219_config_t;

typedef struct {
    float bus_voltage_v;     /**< rail voltage */
    float shunt_voltage_mv;  /**< across the shunt resistor */
    float current_a;         /**< signed: negative while discharging */
    float power_w;           /**< bus voltage * current */
} ina219_data_t;

typedef struct {
    ina219_config_t config;
    float current_lsb_a;     /**< per-LSB current, from max_expected_a */
    float power_lsb_w;       /**< 20 * current_lsb_a (datasheet) */
    SemaphoreHandle_t mutex;
    bool initialized;
    bool healthy;
    uint32_t read_count;
    uint32_t error_count;
} ina219_t;

/**
 * @brief Initialize the INA219, write the calibration, and verify the bus.
 * @return ESP_OK when the device acknowledges and reports a sane voltage.
 */
esp_err_t ina219_init(const ina219_config_t *config);

/** @brief Release the device and free the driver's resources. */
esp_err_t ina219_deinit(void);

/** @brief Read bus voltage, shunt voltage, current and power in one shot. */
esp_err_t ina219_read_once(ina219_data_t *data);

/** @brief True when the last read succeeded and the voltage is plausible. */
bool ina219_is_healthy(void);

/** @brief Driver statistics (reads / errors). */
esp_err_t ina219_get_stats(uint32_t *reads, uint32_t *errors);

#ifdef __cplusplus
}
#endif

#endif // INA219_H
