// GridPrint Station v0 firmware for the Arduino UNO + WiFi (ATmega328P) board
// Board settings: Tools > Board > Arduino Uno. DIP switches 3 and 4 ON, all others OFF.
// Measures the grid frequency once per second from the PC817 pulses on pin D2.
// Prints one line per second:  seconds,frequency_hz,pulses,avg_pulse_width_us,status,d2_low_pct
// d2_low_pct = % of the second pin D2 was LOW. Normal ~50. During an OUTAGE it tells you why:
//   ~100 = D2 stuck LOW  -> sensor output side fault (PC817 or 10k pull-up wiring), not a power cut
//   ~0   = D2 stuck HIGH -> no mains signal reached the sensor (real power cut or transformer side unplugged)
//   else = flickering    -> loose connection or noise

const byte SENSE_PIN = 2;                 // PC817 pin 4 (collector) goes here; pulled up by the ATmega internal pull-up

volatile unsigned long lastFall = 0;      // start of the current pulse (microseconds)
volatile unsigned long firstFall = 0;     // first cycle start in this second
volatile unsigned long lastCycle = 0;     // latest cycle start in this second
volatile unsigned int  nFalls = 0;        // cycles counted this second
volatile unsigned long widthSum = 0;      // total pulse width this second
volatile unsigned int  nWidths = 0;
unsigned long d2Samples = 0, d2Lows = 0;  // pin D2 sampled in loop() to diagnose outages

void onChange() {
  unsigned long t = micros();
  bool low = !(PIND & _BV(PD2));          // fast read of D2
  if (low) {                              // optocoupler switched on: a new grid cycle starts
    if (t - lastFall > 15000UL) {         // ignore bounce (one cycle is about 20000 us)
      if (nFalls == 0) firstFall = t;
      lastCycle = t;
      nFalls++;
      lastFall = t;
    }
  } else if (lastFall != 0 && t - lastFall < 20000UL) {   // pulse ended: record its width
    widthSum += t - lastFall;
    nWidths++;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(SENSE_PIN, INPUT_PULLUP);       // built-in ~35k pull-up; the external 10k / 5V wire is optional now
  attachInterrupt(digitalPinToInterrupt(SENSE_PIN), onChange, CHANGE);
  Serial.println(F("seconds,frequency_hz,pulses,avg_pulse_width_us,status,d2_low_pct"));
}

void loop() {
  static unsigned long nextReport = millis() + 1000;
  d2Samples++;                            // sample D2 thousands of times a second
  if (!(PIND & _BV(PD2))) d2Lows++;
  if ((long)(millis() - nextReport) < 0) return;
  nextReport += 1000;

  noInterrupts();                         // copy this second's data, then reset
  unsigned int  n  = nFalls;
  unsigned long f0 = firstFall, f1 = lastCycle;
  unsigned long wS = widthSum;
  unsigned int  wN = nWidths;
  nFalls = 0; widthSum = 0; nWidths = 0;
  interrupts();

  float freq = 0;
  if (n >= 2) freq = (n - 1) * 1000000.0 / (float)(f1 - f0);   // cycles / time they took
  float width = wN ? (float)wS / wN : 0;
  byte d2pct = d2Samples ? (byte)((d2Lows * 100UL) / d2Samples) : 0;
  d2Samples = 0; d2Lows = 0;

  const char* status = "OK";
  if (n <= 2) status = "OUTAGE";                    // no (or only stray) pulses: power is off
  else if (n < 45 || n > 55) status = "GLITCH";     // missing or extra pulses: switch-over?

  Serial.print(millis() / 1000); Serial.print(',');
  Serial.print(freq, 4);         Serial.print(',');
  Serial.print(n);               Serial.print(',');
  Serial.print(width, 0);        Serial.print(',');
  Serial.print(status);           Serial.print(',');
  Serial.println(d2pct);
}
