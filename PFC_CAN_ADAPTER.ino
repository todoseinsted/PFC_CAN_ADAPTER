#include <SPI.h>
#include <mcp2515.h>

const int CS_PIN = 5; 
MCP2515 mcp2515(CS_PIN);
struct can_frame canMsg;

// Variables de estado
float tensionSet = 0.0;
float corrienteSet = 0.0;
bool pfcEncendido = false;
uint32_t ultimoEnvioControl = 0;

void setup() {
  Serial.begin(115200);
  SPI.begin();
  
  mcp2515.reset();
  mcp2515.setBitrate(CAN_125KBPS, MCP_8MHZ); 
  mcp2515.setNormalMode();
  
  Serial.println("--- Control PFC TonHe TH30F10025C7-WT ---");
  Serial.println("Comandos aceptados:");
  Serial.println("  START,Tension,Corriente,1  (Ej: START,311,10,1)");
  Serial.println("  STOP,1");
}

void enviarC_M_24(float v, float i, bool power) {
  // Identificador para Módulo 1: Prioridad 2, PGN 0x0600, Dest: 01, Source: A0 [cite: 1852]
  canMsg.can_id  = 0x080601A0 | CAN_EFF_FLAG; 
  canMsg.can_dlc = 8;

  // Byte 1: Start (0xAA) / Stop (0x55) [cite: 1952]
  canMsg.data[0] = power ? 0xAA : 0x55; 
  
  // Byte 2: Modo Standby (0x00) [cite: 1952]
  canMsg.data[1] = 0x00;

  // Bytes 3-4: Tension (0.1V/bit) - Little Endian 
  uint16_t v_bits = (uint16_t)(v * 10);
  canMsg.data[2] = v_bits & 0xFF;
  canMsg.data[3] = (v_bits >> 8) & 0xFF;

  // Bytes 5-6: Corriente (0.01A/bit) - Little Endian [cite: 1956]
  uint16_t i_bits = (uint16_t)(i * 100);
  canMsg.data[4] = i_bits & 0xFF;
  canMsg.data[5] = (i_bits >> 8) & 0xFF;

  // Bytes 7-8: Reserva [cite: 1956]
  canMsg.data[6] = 0x00;
  canMsg.data[7] = 0x00;

  mcp2515.sendMessage(&canMsg);

  ultimoEnvioControl = millis();
}

void procesarSerial() {
  if (Serial.available() > 0) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    input.toUpperCase();

    if (input.startsWith("START")) {
      // Formato esperado: START,311,10,1
      int firstComma = input.indexOf(',');
      int secondComma = input.indexOf(',', firstComma + 1);
      int thirdComma = input.indexOf(',', secondComma + 1);

      if (firstComma != -1 && secondComma != -1 && thirdComma != -1) {
        tensionSet = input.substring(firstComma + 1, secondComma).toFloat();
        corrienteSet = input.substring(secondComma + 1, thirdComma).toFloat();
        pfcEncendido = true;
        
        Serial.print(">>> CONFIGURADO: "); 
        Serial.print(tensionSet); Serial.print("V, ");
        Serial.print(corrienteSet); Serial.println("A. Enviando START...");
      }
    } 
    else if (input == "STOP,1") {
      pfcEncendido = false;
      tensionSet = 0;
      corrienteSet = 0;
      Serial.println(">>> COMANDO: STOP Enviado.");
    }
  }
}

void loop() {
  procesarSerial();

  if(millis()-ultimoEnvioControl >= 500){
    // El manual requiere enviar el comando periódicamente para mantener la salida [cite: 1847, 2013]
    enviarC_M_24(tensionSet, corrienteSet, pfcEncendido);
  }

}