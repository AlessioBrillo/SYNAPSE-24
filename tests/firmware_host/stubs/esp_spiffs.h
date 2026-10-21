/* Host stub for esp_spiffs.h: the SPIFFS mount API used by the FIT writer.
 *
 * The writer only needs the mount to succeed and the partition size for a
 * log line, so the stub is a no-op that reports an empty partition. The
 * files themselves are written through the normal stdio calls, which land in
 * the directory the host gate points the writer at.
 */

#ifndef STUB_ESP_SPIFFS_H
#define STUB_ESP_SPIFFS_H

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

typedef struct {
    const char *base_path;
    const char *partition_label;
    size_t max_files;
    bool format_if_mount_failed;
} esp_vfs_spiffs_conf_t;

static inline esp_err_t esp_vfs_spiffs_register(const esp_vfs_spiffs_conf_t *conf)
{
    (void)conf;
    return ESP_OK;
}

static inline esp_err_t esp_spiffs_info(const char *partition_label,
                                        size_t *total_bytes,
                                        size_t *used_bytes)
{
    (void)partition_label;
    if (total_bytes != NULL) {
        *total_bytes = 0;
    }
    if (used_bytes != NULL) {
        *used_bytes = 0;
    }
    return ESP_OK;
}

#endif /* STUB_ESP_SPIFFS_H */
