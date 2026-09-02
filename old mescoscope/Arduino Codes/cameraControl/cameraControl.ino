/* ==========================================================
 * Camera-controller  (Arduino Uno R3, 16 MHz)
 *  ‣ Strobe / FVAL  →  **D2**  (INT0 – rising-edge interrupt)
 *  ‣ Barcode in     →  D8      (polled)
 *  ‣ LEDs           →  D9 (405 nm), D10 (470 nm), D11 (565 nm)
 *  ‣ Serial         115200 baud   → match MATLAB serialport()
 * ========================================================== */

const uint8_t pin_trigIn   = 3;
const uint8_t pin_strobe   = 2;   // INT0
const uint8_t pin_FVal     = 5;
const uint8_t pin_trigOut  = 6;
const uint8_t pin_LVal     = 7;
const uint8_t pin_barCode  = 8;
const uint8_t pin_LED405   = 9;
const uint8_t pin_LED470   = 10;
const uint8_t pin_LED565   = 11;

/* ---------- Globals ---------- */
volatile unsigned long frameNumber = 0;
volatile bool frameJustStarted = false;

bool barcodestate = false;
bool prevBarcode = false;

unsigned long start_us = 0;
unsigned long ts = 0;

int mode = 7;  // LED mode (1–7)
int LEDstatus = 0;

unsigned long LEDonTime = 0;
unsigned long LEDoffTime = 0;
unsigned long LEDonDuration = 8000;        // us
unsigned long postExposureBuffer = 5000;   // us

/* ---------- Function Declarations ---------- */
void onFrameRise();
void controlLEDs(int mode, unsigned long frameNo);
void sendEventInfo(const char* event, const char* flag);

/* ==========================================================
 * setup()
 * ========================================================== */
void setup() {
  Serial.begin(115200);  // match MATLAB

  pinMode(pin_trigIn, INPUT);
  pinMode(pin_strobe, INPUT);
  pinMode(pin_FVal, INPUT);
  pinMode(pin_trigOut, INPUT);
  pinMode(pin_LVal, INPUT);
  pinMode(pin_barCode, INPUT);

  pinMode(pin_LED405, OUTPUT);
  pinMode(pin_LED470, OUTPUT);
  pinMode(pin_LED565, OUTPUT);
  digitalWrite(pin_LED405, LOW);
  digitalWrite(pin_LED470, LOW);
  digitalWrite(pin_LED565, LOW);

  attachInterrupt(digitalPinToInterrupt(pin_strobe), onFrameRise, RISING);

  // Wait for MATLAB to send parameters
  while (Serial.available() == 0) { /* wait */ }
  mode = Serial.parseInt();
  LEDonDuration = Serial.parseInt();
  postExposureBuffer = Serial.parseInt();
  delay(2000);  // Give MATLAB time to arm camera

  start_us = micros();
}

/* ==========================================================
 * loop()
 * ========================================================== */
void loop() {
  ts = micros() - start_us;
  unsigned long now = micros();

  // LED turn-on time check (overflow-safe)
  if (LEDstatus == 1 && (long)(now - LEDonTime) >= 0) {
    LEDstatus = 2;
    controlLEDs(mode, frameNumber);
    sendEventInfo("Frame Number", String(frameNumber).c_str());
  }

  // LED turn-off time check (overflow-safe)
  if (LEDstatus == 2 && (long)(now - LEDoffTime) >= 0) {
    LEDstatus = 0;
    digitalWrite(pin_LED405, LOW);
    digitalWrite(pin_LED470, LOW);
    digitalWrite(pin_LED565, LOW);
  }

  // Barcode edge detection
  prevBarcode = barcodestate;
  barcodestate = digitalRead(pin_barCode);
  if (barcodestate != prevBarcode) {
    sendEventInfo("Barcode", barcodestate ? "On" : "Off");
  }
}

/* ==========================================================
 * Interrupt: strobe rising edge = new frame
 * ========================================================== */
void onFrameRise() {
  frameNumber++;
  unsigned long now = micros();
  LEDonTime = now + postExposureBuffer;
  LEDoffTime = LEDonTime + LEDonDuration;
  LEDstatus = 1;
}

/* ==========================================================
 * controlLEDs(): Light LEDs based on mode and frame number
 * ========================================================== */
void controlLEDs(int mode, unsigned long frameNo) {
  switch (mode) {
    case 1:
      digitalWrite(pin_LED405, HIGH);
      sendEventInfo("LED 405", "On");
      break;
    case 2:
      digitalWrite(pin_LED470, HIGH);
      sendEventInfo("LED 470", "On");
      break;
    case 3:
      digitalWrite(pin_LED565, HIGH);
      sendEventInfo("LED 565", "On");
      break;
    case 4:  // alternate 405 / 470
      if (frameNo & 1) {
        digitalWrite(pin_LED470, HIGH);
        sendEventInfo("LED 470", "On");
      } else {
        digitalWrite(pin_LED405, HIGH);
        sendEventInfo("LED 405", "On");
      }
      break;
    case 5:  // alternate 405 / 565
      if (frameNo & 1) {
        digitalWrite(pin_LED565, HIGH);
        sendEventInfo("LED 565", "On");
      } else {
        digitalWrite(pin_LED405, HIGH);
        sendEventInfo("LED 405", "On");
      }
      break;
    case 6:  // alternate 470 / 565
      if (frameNo & 1) {
        digitalWrite(pin_LED565, HIGH);
        sendEventInfo("LED 565", "On");
      } else {
        digitalWrite(pin_LED470, HIGH);
        sendEventInfo("LED 470", "On");
      }
      break;
    case 7:  // cycle 405 → 470 → 565
      switch (frameNo % 3) {
        case 0:
          digitalWrite(pin_LED405, HIGH);
          sendEventInfo("LED 405", "On");
          break;
        case 1:
          digitalWrite(pin_LED470, HIGH);
          sendEventInfo("LED 470", "On");
          break;
        default:
          digitalWrite(pin_LED565, HIGH);
          sendEventInfo("LED 565", "On");
          break;
      }
      break;
  }
}

/* ==========================================================
 * sendEventInfo(): Send timestamped event log over serial
 * ========================================================== */
void sendEventInfo(const char* event, const char* flag) {
  Serial.print(event);
  Serial.print(" ,");
  Serial.print(flag);
  Serial.print(" ,");
  Serial.println(ts);
}
