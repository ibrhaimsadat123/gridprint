// GridPrint Station uplink v2: firmware for the ESP8266 Wi-Fi chip on the UNO + WiFi board
//
// The ATmega328P measures the grid and prints one line per second:
//     seconds,frequency_hz,pulses,avg_pulse_width_us,status
// This sketch stamps each line with real UTC time (NTP) and sends it to the
// GridPrint server in 5-second packets over one kept-alive HTTPS connection.
//
// Online : packets go straight from RAM to the server (no flash writes).
// Offline: packets are appended to 10-minute files in flash (~22 hours fit).
//          When the server is reachable again the backlog uploads, oldest first,
//          between live packets, and each file is deleted once the server confirms it.
//
// Arduino IDE: Tools > Board > ESP8266 Boards > Generic ESP8266 Module
//              Flash Size: 4MB (FS:3MB OTA:~512KB)
// Upload with DIP switches 5, 6, 7 ON (others OFF).
// Run with DIP switches 1, 2, 3, 4 ON (others OFF) (3+4 let the laptop listen in).

#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClientSecureBearSSL.h>
#include <LittleFS.h>
#include <time.h>
#include <sys/time.h>

// Wi-Fi name/password, server address and token live in secrets.h (kept out of GitHub).
#include "secrets.h"

const int PACKET_SECONDS = 5;          // readings per live packet
const int CHUNK_READINGS = 600;        // readings per offline file (10 minutes)
const char* FS_VERSION = "/v2";        // marker: queue was wiped when v2 was installed

BearSSL::WiFiClientSecure tls;          // one connection, kept alive between packets
HTTPClient http;
bool httpOpen = false;

String inLine;                          // line being received from the ATmega
String packet;                          // readings waiting to be sent
int packetCount = 0;
unsigned long packetFirst = 0;
bool serverUp = false;                  // did the last send succeed?
String chunkName = "";                  // offline file being filled
int chunkReadings = 0;
unsigned long nextBacklogMs = 0, nextRetryMs = 0;

bool clockSet() { return time(nullptr) > 1700000000; }

// ---------------------------------------------------------------- storage
String oldestFile() {
  Dir d = LittleFS.openDir("/q");
  String best = ""; unsigned long bestN = 0xFFFFFFFF;
  while (d.next()) {
    unsigned long n = strtoul(d.fileName().c_str(), nullptr, 10);
    if (n < bestN) { bestN = n; best = "/q/" + d.fileName(); }
  }
  return best;
}

void makeRoom() {
  FSInfo fi; LittleFS.info(fi);
  while (fi.totalBytes - fi.usedBytes < 40000) {      // keep ~40 KB free, drop oldest
    String oldest = oldestFile();
    if (oldest == "") return;
    if (oldest == chunkName) { chunkName = ""; chunkReadings = 0; }
    LittleFS.remove(oldest);
    LittleFS.info(fi);
  }
}

void saveOffline(const String& data, int readings, unsigned long firstSec) {
  makeRoom();
  if (chunkName == "" || chunkReadings >= CHUNK_READINGS || !LittleFS.exists(chunkName)) {
    chunkName = "/q/" + String(firstSec) + ".csv";
    chunkReadings = 0;
  }
  File f = LittleFS.open(chunkName, "a");
  if (f) { f.print(data); f.close(); }
  chunkReadings += readings;
}

// ---------------------------------------------------------------- network
bool ensureHttp() {
  if (httpOpen) return true;
  tls.setInsecure();                    // encrypted; certificate not pinned (v1 simplicity)
  http.setReuse(true);                  // keep-alive: no new TLS handshake per packet
  http.setTimeout(8000);
  if (!http.begin(tls, SERVER_URL)) return false;
  httpOpen = true;
  return true;
}

void dropHttp() { http.end(); tls.stop(); httpOpen = false; }

int post(Stream* s, size_t len, const String* body) {
  if (!ensureHttp()) return -1;
  http.addHeader("Content-Type", "text/csv");
  http.addHeader("X-Station", STATION_ID);
  http.addHeader("X-Token", TOKEN);
  int code = s ? http.sendRequest("POST", s, len) : http.POST(*body);
  if (code > 0) http.getString();       // read the reply so the connection can be reused
  if (code != 200) dropHttp();
  return code;
}

void sendPacket() {
  if (packetCount == 0) return;
  bool sent = false;
  if (WiFi.status() == WL_CONNECTED && (serverUp || (long)(millis() - nextRetryMs) >= 0)) {
    sent = (post(nullptr, 0, &packet) == 200);
    serverUp = sent;
    if (!sent) nextRetryMs = millis() + 30000;   // server/Wi-Fi down: probe again in 30 s
  }
  if (!sent) saveOffline(packet, packetCount, packetFirst);
  packet = ""; packetCount = 0;
}

void uploadBacklogFile() {
  String name = oldestFile();
  if (name == "") return;
  if (name == chunkName) { chunkName = ""; chunkReadings = 0; }   // close the open offline file
  File f = LittleFS.open(name, "r");
  if (!f) { LittleFS.remove(name); return; }
  size_t size = f.size();
  if (size == 0) { f.close(); LittleFS.remove(name); return; }
  int code = post(&f, size, nullptr);   // streamed from flash, not held in RAM
  f.close();
  if (code == 200) LittleFS.remove(name);
  else { serverUp = false; nextRetryMs = millis() + 30000; }
}

// ---------------------------------------------------------------- readings
void handleLine(const String& l) {
  int c1 = l.indexOf(',');
  if (c1 < 0 || l.startsWith("seconds")) return;
  if (!clockSet()) return;              // no real time yet (first few seconds after boot)
  struct timeval tv; gettimeofday(&tv, nullptr);
  char stamp[24];
  snprintf(stamp, sizeof(stamp), "%lu.%03ld", (unsigned long)tv.tv_sec, (long)(tv.tv_usec / 1000));
  if (packetCount == 0) packetFirst = tv.tv_sec;
  packet += stamp; packet += l.substring(c1); packet += '\n';   // unix_time,frequency,pulses,width,status
  packetCount++;
  if (packetCount >= PACKET_SECONDS) sendPacket();
}

void setup() {
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
  LittleFS.begin();
  if (!LittleFS.exists("/q")) LittleFS.mkdir("/q");
  if (!LittleFS.exists(FS_VERSION)) {   // first boot of v2: start fresh, drop the old backlog
    Dir d = LittleFS.openDir("/q");
    while (d.next()) LittleFS.remove("/q/" + d.fileName());
    File m = LittleFS.open(FS_VERSION, "w"); if (m) { m.print("2"); m.close(); }
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.nist.gov");   // UTC
}

void loop() {
  while (Serial.available()) {          // collect lines from the ATmega
    char ch = Serial.read();
    if (ch == '\n') { inLine.trim(); handleLine(inLine); inLine = ""; }
    else if (ch != '\r' && inLine.length() < 80) inLine += ch;
  }
  // between live packets, push one backlog file every 3 s while the server is up
  if (serverUp && WiFi.status() == WL_CONNECTED && (long)(millis() - nextBacklogMs) >= 0) {
    nextBacklogMs = millis() + 3000;
    uploadBacklogFile();
  }
  if (WiFi.status() != WL_CONNECTED && httpOpen) dropHttp();
  yield();
}
