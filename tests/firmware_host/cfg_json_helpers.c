/* Expose the shared header-only whitelist parsers to the host test runner.
 * No ESP-IDF or NimBLE headers are involved. */
#include "cfg_json_helpers.h"

bool host_json_u32(const char *req, const char *key, uint32_t *out) {
    return json_u32(req, key, out);
}

bool host_json_i16_array(const char *req, const char *key, int16_t *out, size_t n) {
    return json_i16_array(req, key, out, n);
}

cfg_json_status_t host_json_only_whitelisted(const char *req) {
    return json_only_whitelisted(req);
}
