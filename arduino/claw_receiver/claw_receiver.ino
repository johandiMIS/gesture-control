/*
 * claw_receiver - drives a claw machine from the gesture controller.
 *
 * Wire protocol (9600 8N1, newline terminated, sent by src/emitter.py):
 *
 *   P<x>,<y>\n   move to position; x and y are integers 0..1000
 *   G<x>,<y>\n   grab at this position
 *   I\n          idle, no player in control - hold still
 *
 * Replies "READY" once after boot, which is what the host waits for before it
 * starts sending. Everything else it prints is diagnostic.
 *
 * The two things in here that are not obvious but matter:
 *
 *   - a link watchdog. If the host stops talking (unplugged cable, crashed
 *     app) the gantry parks itself instead of holding whatever target it last
 *     received. The host sends a heartbeat at least every 250ms precisely so
 *     that silence is unambiguous.
 *   - slew limiting. Targets are approached at a bounded rate, so a corrupted
 *     packet or a hand that teleports across the frame can never slam the
 *     mechanism from one end to the other.
 */

#include <Servo.h>

// ---------------------------------------------------------------- hardware
// TODO: match these to your build. If you drive steppers instead of servos,
// replace applyOutputs() - everything above it is transport, not mechanism.
const uint8_t PIN_X = 9;
const uint8_t PIN_Y = 10;
const uint8_t PIN_CLAW = 11;

const int X_MIN_US = 1000, X_MAX_US = 2000;   // gantry travel limits
const int Y_MIN_US = 1000, Y_MAX_US = 2000;
const int CLAW_OPEN_DEG = 20, CLAW_SHUT_DEG = 90;

// ---------------------------------------------------------------- tuning
const unsigned long LINK_TIMEOUT_MS = 500;    // silence before we assume trouble
const unsigned long UPDATE_MS = 10;           // motion update period
const int SLEW_PER_UPDATE = 12;               // max units (of 1000) per update

// Grab cycle timings. Adjust to the real mechanism.
const unsigned long DROP_MS = 1200;
const unsigned long SHUT_MS = 600;
const unsigned long LIFT_MS = 1400;
const unsigned long RETURN_MS = 2000;

Servo servoX, servoY, servoClaw;

// ---------------------------------------------------------------- state
int targetX = 500, targetY = 500;    // commanded, 0..1000
int posX = 500, posY = 500;          // slew-limited actual
bool havePlayer = false;
unsigned long lastRx = 0;
unsigned long lastUpdate = 0;

enum GrabPhase { IDLE, DROPPING, CLOSING, LIFTING, RETURNING };
GrabPhase phase = IDLE;
unsigned long phaseStart = 0;

char buf[24];
uint8_t buflen = 0;

void setup() {
  Serial.begin(9600);
  servoX.attach(PIN_X);
  servoY.attach(PIN_Y);
  servoClaw.attach(PIN_CLAW);
  servoClaw.write(CLAW_OPEN_DEG);
  lastRx = millis();
  Serial.println(F("READY"));
}

void loop() {
  readSerial();
  runGrab();

  unsigned long now = millis();
  if (now - lastUpdate >= UPDATE_MS) {
    lastUpdate = now;
    stepTowardTarget();
    applyOutputs();
  }

  // Watchdog: park rather than hold a stale target.
  if (now - lastRx > LINK_TIMEOUT_MS && havePlayer) {
    havePlayer = false;
    Serial.println(F("LINK LOST"));
  }
}

// ------------------------------------------------------------- protocol

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      buf[buflen] = '\0';
      handleLine(buf);
      buflen = 0;
    } else if (c != '\r') {
      // Overlong line means we lost sync; drop it and resync on the next
      // newline rather than letting it overflow into the next command.
      if (buflen < sizeof(buf) - 1) {
        buf[buflen++] = c;
      } else {
        buflen = 0;
      }
    }
  }
}

/* Strict parse of "<x>,<y>". Returns false on anything malformed or out of
 * range, so a corrupted packet is ignored instead of moving the claw. */
bool parseXY(const char *s, int *x, int *y) {
  char *end;
  long vx = strtol(s, &end, 10);
  if (end == s || *end != ',') return false;
  const char *second = end + 1;
  long vy = strtol(second, &end, 10);
  if (end == second || *end != '\0') return false;
  if (vx < 0 || vx > 1000 || vy < 0 || vy > 1000) return false;
  *x = (int)vx;
  *y = (int)vy;
  return true;
}

void handleLine(const char *line) {
  if (line[0] == '\0') return;
  lastRx = millis();

  switch (line[0]) {
    case 'P': {
      int x, y;
      if (parseXY(line + 1, &x, &y)) {
        targetX = x;
        targetY = y;
        havePlayer = true;
      }
      break;
    }
    case 'G': {
      int x, y;
      if (parseXY(line + 1, &x, &y) && phase == IDLE) {
        targetX = x;
        targetY = y;
        havePlayer = true;
        // Snap to the grab coordinates: the host already decided this is the
        // spot, and slewing there first would drop the claw somewhere else.
        posX = x;
        posY = y;
        beginGrab();
      }
      break;
    }
    case 'I':
      havePlayer = false;
      break;
    default:
      break;   // unknown opcode: ignore, stay in sync
  }
}

// ------------------------------------------------------------- motion

void stepTowardTarget() {
  if (phase != IDLE) return;      // the grab cycle owns position while it runs
  if (!havePlayer) return;        // no player: hold where we are
  posX += constrain(targetX - posX, -SLEW_PER_UPDATE, SLEW_PER_UPDATE);
  posY += constrain(targetY - posY, -SLEW_PER_UPDATE, SLEW_PER_UPDATE);
}

void applyOutputs() {
  servoX.writeMicroseconds(map(posX, 0, 1000, X_MIN_US, X_MAX_US));
  servoY.writeMicroseconds(map(posY, 0, 1000, Y_MIN_US, Y_MAX_US));
}

// ------------------------------------------------------------- grab cycle

void beginGrab() {
  phase = DROPPING;
  phaseStart = millis();
  Serial.print(F("GRAB "));
  Serial.print(posX);
  Serial.print(',');
  Serial.println(posY);
  // TODO: start lowering the claw here (winch motor on, or hoist servo down).
}

void runGrab() {
  if (phase == IDLE) return;
  unsigned long elapsed = millis() - phaseStart;

  switch (phase) {
    case DROPPING:
      if (elapsed >= DROP_MS) {
        servoClaw.write(CLAW_SHUT_DEG);
        phase = CLOSING;
        phaseStart = millis();
      }
      break;
    case CLOSING:
      if (elapsed >= SHUT_MS) {
        phase = LIFTING;
        phaseStart = millis();
        // TODO: raise the claw.
      }
      break;
    case LIFTING:
      if (elapsed >= LIFT_MS) {
        phase = RETURNING;
        phaseStart = millis();
        posX = 0;            // travel to the prize chute
        posY = 0;
      }
      break;
    case RETURNING:
      if (elapsed >= RETURN_MS) {
        servoClaw.write(CLAW_OPEN_DEG);
        phase = IDLE;
        // Re-centre the commanded target on where we actually are, so control
        // resumes smoothly instead of racing back to a stale position.
        targetX = posX;
        targetY = posY;
        Serial.println(F("DONE"));
      }
      break;
    default:
      break;
  }
}
