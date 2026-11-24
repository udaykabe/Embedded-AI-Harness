#pragma once

#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

esp_err_t nvs_store_init(void);
esp_err_t nvs_store_set_wifi(const char *ssid, const char *password);
bool      nvs_store_get_wifi(char *ssid, size_t ssid_len, char *password, size_t pass_len);
esp_err_t nvs_store_erase_wifi(void);

/* A named access point the DUT raises instead of joining anything, so the
 * bench can be tested as a *station* without an external network.
 *
 * The bench has one radio: it cannot be the AP the tests join. Its own
 * provisioning portal is open by design, which leaves the WPA2 paths — a
 * correct passphrase accepted, a wrong one refused — with nothing to aim
 * at. This entry gives the same board, the one already under test, a second
 * job. Empty password means an open AP.
 *
 * Set through the serial console, and cleared by `nvs_store_set_wifi`: a
 * DUT that has just been handed credentials is going back to being a
 * station, so provisioning always returns it to normal without a second
 * command anyone could forget. */
esp_err_t nvs_store_set_test_ap(const char *ssid, const char *password);
bool      nvs_store_get_test_ap(char *ssid, size_t ssid_len,
                                char *password, size_t pass_len);
esp_err_t nvs_store_clear_test_ap(void);

/* The MQTT broker the DUT was told to use, entered in the captive portal
 * alongside the credentials. Empty means "not configured": the DUT then
 * falls back to the gateway of its own DHCP lease, which on this bench is
 * the bench itself. */
esp_err_t nvs_store_set_broker(const char *uri);
bool      nvs_store_get_broker(char *uri, size_t uri_len);

/* A transmit-power override, in esp_wifi's quarter-dBm units (8..84).
 *
 * The default is deliberately low (see wifi_prov.c). Some benches cannot
 * hear it: a Pi in a metal enclosure, or with a USB 3 device beside its
 * antenna, loses a 5 dBm partner entirely while hearing ordinary networks
 * fine. This lets such a bench turn the partner up without a rebuild.
 *
 * Stored, because provisioning reboots the board and an override that did
 * not survive that would be gone before the first test used it. Not
 * touched by `forget` or by new credentials: it describes the bench the
 * board sits on, not the network it joins. */
esp_err_t nvs_store_set_txpower(int8_t qdbm);
bool      nvs_store_get_txpower(int8_t *qdbm);
esp_err_t nvs_store_clear_txpower(void);

/* Channel for the partner's own AP (portal and `testap`). Stored for the same
 * reason as the tx power, and for the same kind of bench: one whose
 * neighbours sit on the default channel. A cable modem's WiFi a foot from
 * the Pi, on channel 1 at -27 dBm, left the Pi's station timing out on this
 * partner's channel-1 AP while every channel-6 link worked. */
esp_err_t nvs_store_set_apchan(uint8_t chan);
bool      nvs_store_get_apchan(uint8_t *chan);
esp_err_t nvs_store_clear_apchan(void);
