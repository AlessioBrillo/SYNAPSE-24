/* Host glue for the FIT writer gate.
 *
 * firmware/esp32_band/fit/fit_writer.c is compiled on the build host so the
 * bytes it writes can be parsed by the project's canonical FIT reader (and by
 * a standard parser) instead of being taken on trust.
 *
 * The glue is a standalone program (not a shared library): the build host
 * refuses to load freshly built DLLs (Windows Application Control), so the
 * gate drives the writer through its own entry point and inspects the file
 * it leaves behind.
 */

#include <stdint.h>
#include <stdio.h>

#include "esp_err.h"
#include "esp_timer.h"
#include "fit_writer.h"

const char *esp_err_to_name(esp_err_t e)
{
    (void)e;
    return "stub";
}

int64_t esp_timer_get_time(void)
{
    /* Deterministic clock: the FIT writer only uses it for the session
     * timestamps, which the test supplies explicitly. */
    static int64_t mock_us = 1000000;
    mock_us += 1000;
    return mock_us;
}

int main(void)
{
    fit_session_config_t config = {0};
    config.sport_type = 0;
    config.start_time_utc = 1000000;

    if (synapse_fit_init() != ESP_OK) {
        fprintf(stderr, "synapse_fit_init failed\n");
        return 1;
    }
    if (synapse_fit_start_session(&config) != ESP_OK) {
        fprintf(stderr, "synapse_fit_start_session failed\n");
        return 2;
    }
    for (int i = 0; i < 3; i++) {
        synapse_feature_stream_t feature = {0};
        feature.version = SYNAPSE_FEATURE_STREAM_VERSION;
        feature.sequence = (uint16_t)i;
        feature.heart_rate = (uint16_t)(60 + i);
        feature.rr_interval_ms = 0xFFFF;
        feature.ppg_sqi = 200;
        feature.ppg_map = 50;
        feature.ecg_quality = 220;
        feature.stress_level = 1;
        feature.motion_level = 0xFF;
        feature.battery_level = 80;
        feature.cadence = 0xFFFF;
        feature.temperature = 0x7FFF;
        feature.rmssd_ms = 37;
        feature.sdnn_ms = 42;
        if (synapse_fit_write_from_feature(&feature, NULL, NULL, NULL, NULL, NULL,
                                           NULL, NULL, NULL, NULL, NULL) != ESP_OK) {
            fprintf(stderr, "synapse_fit_write_from_feature failed\n");
            return 3;
        }
    }
    if (synapse_fit_stop_session(NULL) != ESP_OK) {
        fprintf(stderr, "synapse_fit_stop_session failed\n");
        return 4;
    }
    return 0;
}
