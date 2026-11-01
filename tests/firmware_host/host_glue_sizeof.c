/* Exposes sizeof() of the wire structs, compiled with the real header: the
 * documented sizes are only worth what the compiler says. */
#include <stddef.h>

#include "ble_gatt_server.h"

size_t host_sizeof_feature_v1(void)
{
    return sizeof(synapse_feature_stream_t);
}
