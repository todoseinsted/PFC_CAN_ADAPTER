# Control del PFC TonHe desde un Raspberry PLC 19R

Este proyecto permite encender, configurar y detener un módulo de potencia
**TonHe TH30F10025C7-WT** desde un **Industrial Shields Raspberry PLC 19R**.

> **Importante:** el programa controla el PFC conectado al PLC. No enciende ni
> apaga la alimentación del propio PLC. Para apagar físicamente el PLC se debe
> actuar sobre su fuente de alimentación siguiendo el procedimiento de la
> instalación.

## Hardware utilizado

- Industrial Shields Raspberry PLC 19R con Raspberry Pi OS/Debian.
- Módulo CAN HW-184 con controlador MCP2515 y cristal de 8 MHz.
- Conversor de nivel entre las señales SPI de 3,3 V del PLC y los 5 V del
  HW-184.
- Conexión CAN-H y CAN-L entre el HW-184 y el PFC.
- Terminación correcta del bus CAN, normalmente 120 ohmios en cada extremo.

El HW-184 está conectado al bus SPI 0, chip-select 1, disponible en Linux como:

```text
/dev/spidev0.1
```

La línea `INT` no se utiliza. El programa controla el MCP2515 directamente por
SPI y no necesita una interfaz Linux `can0`, `python-can` ni SocketCAN.

## Cómo funciona

El programa `Control-PFC-SPI.py`:

1. Reinicia el MCP2515.
2. Configura CAN clásico a 125 kbit/s para un cristal de 8 MHz.
3. Coloca el MCP2515 en modo normal con transmisión *one-shot*.
4. Envía al PFC una trama de control cada 500 ms.
5. Utiliza el identificador CAN extendido `0x080601A0`.
6. Arranca siempre en estado seguro, enviando `STOP`.
7. Al salir normalmente, envía tres tramas adicionales de `STOP`.

La transmisión *one-shot* evita que el MCP2515 retransmita indefinidamente si
el PFC está desconectado o no responde. El siguiente ciclo de 500 ms realiza un
nuevo intento.

## Límites admitidos

El programa rechaza automáticamente consignas que estén fuera de estos
límites:

| Parámetro | Rango |
|---|---:|
| Tensión de salida | 150 a 1000 V |
| Corriente de salida | 2 a 120 A |
| Potencia solicitada | Máximo 30 kW |

Que una consigna esté dentro de estos límites no significa que sea segura para
la carga conectada. La tensión y la corriente deben seleccionarse de acuerdo
con el equipo, el cableado, las protecciones y el procedimiento de ensayo.

## Requisitos de software

El PLC necesita Python 3 y el módulo `spidev`. Para comprobarlo:

```bash
python3 -c "import spidev; print('spidev disponible')"
```

Si no estuviera instalado:

```bash
sudo apt update
sudo apt install python3-spidev
```

El usuario que ejecuta el programa debe tener permiso para utilizar
`/dev/spidev0.1`. En el PLC configurado para este proyecto, el usuario
`nferraro` pertenece al grupo `spi`.

## Prueba segura antes de encender

Antes de solicitar tensión, se recomienda ejecutar una prueba que transmita
únicamente comandos de parada:

```bash
python3 /home/nferraro/Documents/Control-PFC-SPI.py --stop-test 4
```

El resultado esperado contiene mensajes como:

```text
MCP2515: modo TX directo, sin depender de MISO ni de INT
STOP 1/4 transmitido (TX directo)
STOP 2/4 transmitido (TX directo)
```

Esta prueba no habilita la salida del PFC.

## Uso interactivo

Iniciar el controlador:

```bash
python3 /home/nferraro/Documents/Control-PFC-SPI.py
```

Al comenzar se muestra:

```text
--- Control PFC TonHe por SPI/MCP2515 ---
Comandos: START,<tension>,<corriente> | STOP | STATUS | QUIT
Estado inicial seguro: STOP
```

### Encender el PFC

El comando tiene este formato:

```text
START,<tensión en voltios>,<corriente en amperios>
```

Ejemplo para solicitar 311 V y un límite de 10 A:

```text
START,311,10
```

Si los valores son válidos, se muestra:

```text
START preparado: 311.0 V, 10.00 A
```

Desde ese momento, la trama `START` se transmite cada 500 ms hasta recibir un
comando `STOP`, finalizar el programa o producirse un error.

### Apagar el PFC

Escribir:

```text
STOP
```

El programa cambia inmediatamente la consigna a cero y continúa enviando la
trama de parada cada 500 ms.

### Consultar la consigna local

Escribir:

```text
STATUS
```

`STATUS` muestra la orden que el PLC está intentando transmitir. No confirma la
tensión real ni las alarmas internas del PFC, porque esta instalación funciona
en modo de transmisión directa y no depende del retorno MISO ni de `INT`.

### Finalizar el programa

Escribir:

```text
QUIT
```

También se puede pulsar `Ctrl+C`. En ambos casos el programa intenta enviar
tres tramas `STOP` antes de cerrar el dispositivo SPI.

## Secuencia recomendada de operación

1. Comprobar que no haya personas manipulando la salida ni partes energizadas.
2. Verificar carga, protecciones, puesta a tierra y parada de emergencia.
3. Encender el PLC y el PFC según el procedimiento eléctrico de la instalación.
4. Ejecutar `--stop-test 4`.
5. Iniciar el modo interactivo.
6. Introducir la consigna `START` requerida.
7. Observar los indicadores y las mediciones externas del PFC.
8. Escribir `STOP` antes de intervenir sobre el equipo.
9. Escribir `QUIT` para cerrar el programa.

El software no reemplaza una parada de emergencia cableada ni las protecciones
eléctricas requeridas para una fuente de hasta 1000 V y 30 kW.

## Solución de problemas

### No existe `/dev/spidev0.1`

Comprobar que SPI esté habilitado y que el PLC conserve su configuración de
hardware:

```bash
ls -l /dev/spidev*
```

En este PLC, `spi0.0` está ocupado por el Ethernet W5500 interno y el HW-184
debe utilizar `spi0.1`.

### Error de permisos

Comprobar los grupos del usuario:

```bash
id
```

Debe aparecer el grupo `spi`.

### El PFC no arranca

Comprobar, con el equipo en condición segura:

- Alimentación del PFC y ausencia de alarmas.
- CAN-H y CAN-L sin invertir.
- Masa/referencia y aislamiento según el esquema eléctrico.
- Terminación del bus CAN.
- Cristal del HW-184 marcado `8.000`.
- Conversión de nivel en `SCK`, `SI/MOSI`, `SO/MISO` y `CS`.
- Que no haya otra aplicación utilizando `/dev/spidev0.1`.

### Hay dudas sobre el estado de salida

Enviar `STOP`, finalizar con `QUIT` y verificar la ausencia de tensión con un
instrumento apropiado antes de tocar el circuito. No utilizar solamente los
mensajes de consola como confirmación de ausencia de tensión.

## Archivo Arduino original

`PFC_CAN_ADAPTER.ino` corresponde al prototipo basado en ESP32/Arduino. El
archivo utilizado por el Raspberry PLC 19R es `Control-PFC-SPI.py`.
