#include <Adafruit_NeoPixel.h>

#define OUT_PIN (0)   /* #0 is Digital 0 */
#define PIXEL_PIN (1) /* #1 is Digital 1 */
#define AUDIO_PIN (1) /* #2 is Analog 1 */
#define SAMPLE_WINDOW (50)

Adafruit_NeoPixel strip = Adafruit_NeoPixel(64, PIXEL_PIN, NEO_GRB + NEO_KHZ800);
unsigned int sample;
float dB;
float adB;
int x;
int i;


void setAllPixels(int r, int g, int b)
{
  int i;
  for (i = 0; i < 64; i++) {
    strip.setPixelColor(i, r, g, b);
  }
  strip.show();
}

void setup() {
  strip.begin();
  strip.show();
  pinMode(OUT_PIN, OUTPUT);
}

unsigned long timeout = millis();

void loop() {
  unsigned long startMillis = millis();
  unsigned int peakToPeak = 0;
  unsigned int signalMax = 0;
  unsigned int signalMin = 1024;

  while (millis() - startMillis < SAMPLE_WINDOW) {
    sample = analogRead(AUDIO_PIN);
    /* Ignore bad readings */
    if (sample < 0 || sample > 1023) {
      continue;
    }
    if (sample > signalMax) signalMax = sample;
    if (sample < signalMin) signalMin = sample;
  }
  peakToPeak = signalMax - signalMin;
  dB = 20.0 * log10(((float)peakToPeak + 1.0) / 1024.0);
  adB = (-20.0 * log10(1.0 / 1024.0)) + dB;
  /* adB now ranges from 0 (0Vpp) or 60.205 (3.3Vpp) */
  x = (int)adB;

  if (x > 40) {
    digitalWrite(OUT_PIN, 1);
    timeout = millis() + 5000;
  }

  if (millis() > timeout) {
    digitalWrite(OUT_PIN, 0);
  }

  if (x > 64) {
    setAllPixels(255, 0, 0);
  } else if (x < 0) {
    setAllPixels(0, 0, 255);
  } else if (x == 0) {
    setAllPixels(255, 0, 255);
  } else {
    setAllPixels(0, 0, 0);
    for (i = 0; i < x; i++) {
      strip.setPixelColor(i, 0, 255, 0);
    }
    strip.show();
  }
}
