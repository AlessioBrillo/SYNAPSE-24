/* Host wrappers over firmware/esp32_band/config/synapse_config_limits.h (static inline there). */
#include <stdbool.h>
#include <stdint.h>

#include "synapse_config_limits.h"

int host_gpio_ok(int pin, int optional) { return synapse_cfg_gpio_ok(pin, optional != 0) ? 1 : 0; }
int host_i2c_addr_ok(int addr) { return synapse_cfg_i2c_addr_ok(addr) ? 1 : 0; }
int host_i2c_bus_ok(int sda, int scl, uint32_t freq) { return synapse_cfg_i2c_bus_ok(sda, scl, freq) ? 1 : 0; }
int host_ppg_codes_ok(unsigned sr, unsigned pw, unsigned range)
{
    return synapse_cfg_ppg_codes_ok(sr, pw, range) ? 1 : 0;
}
int host_imu_ok(int a_fsr, int g_fsr, int a_odr, int g_odr)
{
    return synapse_cfg_imu_ok(a_fsr, g_fsr, a_odr, g_odr) ? 1 : 0;
}
