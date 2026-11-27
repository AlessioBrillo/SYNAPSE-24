/**
 * @file synapse_config_json.h
 * @brief Pure readers for the BLE device-config JSON whitelist.
 *
 * Production header: the firmware's cfg_access() and the host parser tests
 * compile the SAME rules, so a host fix cannot drift from the shipped image.
 * Everything is `static inline`, so the ESP-IDF component needs no extra
 * source file. The host tests reach it through the shim
 * ``tests/firmware_host/cfg_json_helpers.h`` without any NimBLE/ESP-IDF headers.
 *
 * Only <string.h>, <stdlib.h>, <errno.h>, <stdint.h>, <stdbool.h> and
 * <limits.h> are used. No stdio, no ESP-IDF.
 */
#ifndef SYNAPSE_CONFIG_JSON_H
#define SYNAPSE_CONFIG_JSON_H

#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    CFG_JSON_OK = 0,
    CFG_JSON_ERR_MALFORMED,
    CFG_JSON_ERR_DUPLICATE_KEY,
    CFG_JSON_ERR_UNKNOWN_KEY,
} cfg_json_status_t;

/** Skip JSON insignificant whitespace in place. */
static inline void cfg_json_skip_ws(const char **p) {
    while (**p == ' ' || **p == '\t' || **p == '\r' || **p == '\n') (*p)++;
}

/** Build the exact `"key":` token without snprintf (stdio is not allowed here). */
static inline bool cfg_json_key_pattern(const char *key, char *out, size_t cap) {
    if (key == NULL || out == NULL) return false;
    const size_t klen = strlen(key);
    if (klen + 4u > cap) return false;
    out[0] = '"';
    memcpy(out + 1, key, klen);
    out[klen + 1] = '"';
    out[klen + 2] = ':';
    out[klen + 3] = '\0';
    return true;
}

/**
 * Read one unsigned decimal field.
 *
 * A leading sign is refused explicitly: strtoul() accepts `-N` and wraps it,
 * so on the ESP32 (where unsigned long is 32-bit) `{"battery_mah":-4294966296}`
 * used to become 1000 and be committed. errno/ERANGE catches values beyond
 * unsigned long; the ULONG_MAX guard catches values that fit unsigned long on
 * a 64-bit host but not in uint32_t (the same check is compiled out on the
 * 32-bit firmware, where the comparison would be dead).
 */
static inline bool json_u32(const char *req, const char *key, uint32_t *out) {
    if (req == NULL || key == NULL || out == NULL) return false;
    char pattern[40];
    if (!cfg_json_key_pattern(key, pattern, sizeof(pattern))) return false;
    const char *p = strstr(req, pattern);
    if (p == NULL) return false;
    p += strlen(pattern);
    cfg_json_skip_ws(&p);
    if (*p == '-' || *p == '+') return false;
    errno = 0;
    char *end = NULL;
    const unsigned long value = strtoul(p, &end, 10);
    if (errno == ERANGE || end == p) return false;
#if ULONG_MAX > 0xFFFFFFFFu
    if (value > 0xFFFFFFFFu) return false;  /* uint32_t overflow on a 64-bit host */
#endif
    (void)value;
    /* `31abc` is a malformed field, not 31. */
    if (*end != '\0' && *end != ',' && *end != '}' && *end != ' ' &&
        *end != '\t' && *end != '\r' && *end != '\n') return false;
    *out = (uint32_t)value;
    return true;
}

/** Read a 3-element (or n-element) int16 array; require `[`, commas and `]`. */
static inline bool json_i16_array(const char *req, const char *key, int16_t *out, size_t n) {
    if (req == NULL || key == NULL || out == NULL || n == 0) return false;
    char pattern[40];
    if (!cfg_json_key_pattern(key, pattern, sizeof(pattern))) return false;
    const char *p = strstr(req, pattern);
    if (p == NULL) return false;
    p += strlen(pattern);
    cfg_json_skip_ws(&p);
    if (*p != '[') return false;
    p++;
    for (size_t i = 0; i < n; i++) {
        cfg_json_skip_ws(&p);
        if (i > 0) {
            if (*p != ',') return false;  /* missing comma between elements */
            p++;
            cfg_json_skip_ws(&p);
        }
        errno = 0;
        char *end = NULL;
        const long value = strtol(p, &end, 10);
        if (errno == ERANGE || end == p || value < -32768L || value > 32767L) return false;
        out[i] = (int16_t)value;
        p = end;
    }
    cfg_json_skip_ws(&p);
    if (*p != ']') return false;
    p++;
    cfg_json_skip_ws(&p);
    return *p == '\0' || *p == ',' || *p == '}';
}

/**
 * Validate the whole payload: one balanced {...} object, only whitelisted,
 * non-duplicated keys, and no quoted token in value position (every value is
 * a number or an array). Failure kind is returned so cfg_access() can log a
 * distinct message for malformed JSON, a duplicate key and an unknown key.
 */
static inline cfg_json_status_t json_only_whitelisted(const char *req) {
    if (req == NULL) return CFG_JSON_ERR_MALFORMED;
    /* The complete device-config whitelist, shared by firmware and host tests. */
    static const char *const allowed[] = {
        "led_red", "led_ir", "gain_x100", "feature_rate_hz",
        "battery_mah", "accel_bias_mg", "gyro_bias_mdps",
    };
    const size_t n_allowed = sizeof(allowed) / sizeof(allowed[0]);
    bool seen[sizeof(allowed) / sizeof(allowed[0])] = {false};

    const char *p = req;
    cfg_json_skip_ws(&p);
    if (*p != '{') return CFG_JSON_ERR_MALFORMED;
    int depth = 0;
    while (*p != '\0') {
        const char c = *p;
        if (c == '"') {
            const char *start = p;
            const char *end = strchr(p + 1, '"');
            if (end == NULL) return CFG_JSON_ERR_MALFORMED;
            /* A quoted token is a key only when preceded by `{` or `,` and
             * followed by `:`. Anything else is a quoted value (malformed). */
            const char *before = start;
            while (before > req) {
                --before;
                if (*before != ' ' && *before != '\t' && *before != '\r' && *before != '\n') {
                    break;
                }
            }
            if (*before != '{' && *before != ',') return CFG_JSON_ERR_MALFORMED;
            const char *after = end + 1;
            cfg_json_skip_ws(&after);
            if (*after != ':') return CFG_JSON_ERR_MALFORMED;

            const size_t len = (size_t)(end - start - 1);
            bool found = false;
            for (size_t i = 0; i < n_allowed; i++) {
                if (strlen(allowed[i]) == len &&
                    strncmp(start + 1, allowed[i], len) == 0) {
                    if (seen[i]) return CFG_JSON_ERR_DUPLICATE_KEY;
                    seen[i] = true;
                    found = true;
                    break;
                }
            }
            if (!found) return CFG_JSON_ERR_UNKNOWN_KEY;
            p = end + 1;
            continue;
        }
        if (c == '{') {
            depth++;
        } else if (c == '}') {
            if (--depth < 0) return CFG_JSON_ERR_MALFORMED;
            if (depth == 0) {
                const char *q = p + 1;
                cfg_json_skip_ws(&q);
                return *q == '\0' ? CFG_JSON_OK : CFG_JSON_ERR_MALFORMED;
            }
        } else if (!((c >= '0' && c <= '9') || c == '-' || c == '[' || c == ']' ||
                     c == ',' || c == ':' || c == ' ' || c == '\t' || c == '\r' || c == '\n')) {
            return CFG_JSON_ERR_MALFORMED;  /* true/null/bare word/junk */
        }
        p++;
    }
    return CFG_JSON_ERR_MALFORMED;  /* no closing brace */
}

#ifdef __cplusplus
}
#endif

#endif /* SYNAPSE_CONFIG_JSON_H */
