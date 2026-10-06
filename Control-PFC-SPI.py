#!/usr/bin/env python3
"""Control seguro del PFC TonHe mediante un MCP2515 conectado por SPI.

Diseñado para el Raspberry PLC 19R observado en campo:
  - SPI bus 0, chip-select 1 (/dev/spidev0.1)
  - HW-184 con cristal de 8 MHz
  - CAN clásico a 125 kbit/s
  - Sin línea INT: transmisión y recepción por sondeo de registros

El programa original Control-PFC.py (SocketCAN) no es modificado.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable

import spidev


CONTROL_CAN_ID = 0x080601A0
STATUS_CAN_ID = 0x1801A001
SEND_PERIOD_SECONDS = 0.5
DEFAULT_RECOVERY_INTERVAL_SECONDS = 5.0
MAX_CONSECUTIVE_TX_ERRORS = 2

# Límites publicados para el TH30F10025C7-WT. Además se limita la potencia
# solicitada a la potencia nominal del módulo.
MIN_START_VOLTAGE = 150.0
MAX_START_VOLTAGE = 1000.0
MIN_START_CURRENT = 2.0
MAX_START_CURRENT = 120.0
MAX_OUTPUT_POWER_W = 30_000.0


class MCP2515Error(RuntimeError):
    """Error de inicialización o acceso al MCP2515."""


class CANTransmissionError(MCP2515Error):
    """La trama no pudo transmitirse correctamente."""


@dataclass(frozen=True)
class CANFrame:
    arbitration_id: int
    data: bytes
    is_extended: bool


class MCP2515:
    # Instrucciones SPI
    INSTRUCTION_RESET = 0xC0
    INSTRUCTION_READ = 0x03
    INSTRUCTION_WRITE = 0x02
    INSTRUCTION_BIT_MODIFY = 0x05
    INSTRUCTION_RTS_TX0 = 0x81

    # Registros
    CANSTAT = 0x0E
    CANCTRL = 0x0F
    TEC = 0x1C
    REC = 0x1D
    CNF3 = 0x28
    CNF2 = 0x29
    CNF1 = 0x2A
    CANINTE = 0x2B
    CANINTF = 0x2C
    EFLG = 0x2D
    TXB0CTRL = 0x30
    TXB0SIDH = 0x31
    RXB0CTRL = 0x60
    RXB0SIDH = 0x61
    RXB1CTRL = 0x70
    RXB1SIDH = 0x71

    CANCTRL_REQOP_MASK = 0xE0
    CANCTRL_OSM = 0x08
    CANSTAT_OPMOD_MASK = 0xE0
    MODE_NORMAL = 0x00
    MODE_CONFIG = 0x80

    TXREQ = 0x08
    TX_ERROR_MASK = 0x70

    # Valores contrastados con la biblioteca autowp/arduino-mcp2515.
    # MCP2515 a 8 MHz, CAN clásico a 125 kbit/s.
    CNF_125K_8MHZ = (0x01, 0xB1, 0x85)  # CNF1, CNF2, CNF3

    def __init__(
        self,
        bus: int = 0,
        device: int = 1,
        spi_speed_hz: int = 500_000,
        readback: bool = False,
    ) -> None:
        self._spi = spidev.SpiDev()
        self._spi.open(bus, device)
        self._spi.max_speed_hz = spi_speed_hz
        self._spi.mode = 0
        self._spi.bits_per_word = 8
        self._lock = threading.Lock()
        self._closed = False
        self._readback = readback

    def close(self) -> None:
        if not self._closed:
            self._spi.close()
            self._closed = True

    def _transfer(self, values: Iterable[int]) -> list[int]:
        if self._closed:
            raise MCP2515Error("El dispositivo SPI está cerrado")
        return self._spi.xfer2(list(values))

    def reset(self) -> None:
        with self._lock:
            self._transfer([self.INSTRUCTION_RESET])
        time.sleep(0.02)

    def read_register(self, address: int) -> int:
        with self._lock:
            return self._transfer([self.INSTRUCTION_READ, address, 0x00])[2]

    def read_registers(self, address: int, count: int) -> bytes:
        with self._lock:
            response = self._transfer(
                [self.INSTRUCTION_READ, address] + [0x00] * count
            )
        return bytes(response[2:])

    def write_register(self, address: int, value: int) -> None:
        with self._lock:
            self._transfer([self.INSTRUCTION_WRITE, address, value & 0xFF])

    def write_registers(self, address: int, values: Iterable[int]) -> None:
        payload = [value & 0xFF for value in values]
        with self._lock:
            self._transfer([self.INSTRUCTION_WRITE, address] + payload)

    def bit_modify(self, address: int, mask: int, value: int) -> None:
        with self._lock:
            self._transfer(
                [self.INSTRUCTION_BIT_MODIFY, address, mask & 0xFF, value & 0xFF]
            )

    def _set_mode(self, mode: int, one_shot: bool = False) -> None:
        value = mode | (self.CANCTRL_OSM if one_shot else 0)
        self.bit_modify(
            self.CANCTRL,
            self.CANCTRL_REQOP_MASK | self.CANCTRL_OSM,
            value,
        )
        deadline = time.monotonic() + 0.100
        while time.monotonic() < deadline:
            if self.read_register(self.CANSTAT) & self.CANSTAT_OPMOD_MASK == mode:
                return
            time.sleep(0.002)
        canstat = self.read_register(self.CANSTAT)
        canctrl = self.read_register(self.CANCTRL)
        raise MCP2515Error(
            f"El MCP2515 no cambió de modo: CANSTAT=0x{canstat:02X}, "
            f"CANCTRL=0x{canctrl:02X}"
        )

    def initialize(self) -> None:
        self.reset()
        cnf1, cnf2, cnf3 = self.CNF_125K_8MHZ
        self.write_register(self.CNF1, cnf1)
        self.write_register(self.CNF2, cnf2)
        self.write_register(self.CNF3, cnf3)

        # No se utiliza la salida INT. Se aceptan tramas estándar y extendidas
        # y se consultan los indicadores RX0IF/RX1IF por SPI.
        self.write_register(self.CANINTE, 0x00)
        self.write_register(self.CANINTF, 0x00)
        self.write_register(self.RXB0CTRL, 0x20)  # sólo estándar; ignora estado extendido
        self.write_register(self.RXB1CTRL, 0x20)
        self.write_register(self.TXB0CTRL, 0x00)

        # One-shot evita que una ausencia de ACK deje al MCP2515 retransmitiendo
        # indefinidamente. El heartbeat de 500 ms aporta el reintento de aplicación.
        if self._readback:
            # Los registros CNF deben verificarse en modo configuración.
            expected = {self.CNF1: cnf1, self.CNF2: cnf2, self.CNF3: cnf3}
            for address, value in expected.items():
                observed = self.read_register(address)
                if observed != value:
                    raise MCP2515Error(
                        f"Verificación fallida en 0x{address:02X}: "
                        f"esperado 0x{value:02X}, leído 0x{observed:02X}"
                    )
            self._set_mode(self.MODE_NORMAL, one_shot=True)
        else:
            # El conversor bidireccional instalado deforma ocasionalmente MISO.
            # Se escribe el modo directamente y no se depende de lecturas SPI.
            self.write_register(self.CANCTRL, self.CANCTRL_OSM)
            time.sleep(0.020)

    @staticmethod
    def _encode_extended_id(arbitration_id: int) -> bytes:
        if not 0 <= arbitration_id <= 0x1FFFFFFF:
            raise ValueError("El identificador CAN extendido debe tener 29 bits")
        low = arbitration_id & 0xFFFF
        high = arbitration_id >> 16
        return bytes(
            [
                (high >> 5) & 0xFF,
                (high & 0x03) | ((high & 0x1C) << 3) | 0x08,
                (low >> 8) & 0xFF,
                low & 0xFF,
            ]
        )

    @staticmethod
    def _decode_id(header: bytes) -> tuple[int, bool]:
        sidh, sidl, eid8, eid0 = header[:4]
        arbitration_id = (sidh << 3) | (sidl >> 5)
        is_extended = bool(sidl & 0x08)
        if is_extended:
            arbitration_id = (arbitration_id << 2) | (sidl & 0x03)
            arbitration_id = (arbitration_id << 8) | eid8
            arbitration_id = (arbitration_id << 8) | eid0
        return arbitration_id, is_extended

    def _abort_transmission(self) -> None:
        self.bit_modify(self.CANCTRL, 0x10, 0x10)
        time.sleep(0.005)
        self.bit_modify(self.CANCTRL, 0x10, 0x00)
        self.bit_modify(self.TXB0CTRL, self.TXREQ, 0x00)

    def send_extended(
        self,
        arbitration_id: int,
        data: bytes,
        timeout: float = 0.100,
    ) -> dict[str, int]:
        if len(data) > 8:
            raise ValueError("CAN clásico admite como máximo 8 bytes")

        header = self._encode_extended_id(arbitration_id)
        if not self._readback:
            self.write_register(self.TXB0CTRL, 0x00)
            self.write_registers(self.TXB0SIDH, header + bytes([len(data)]) + data)
            with self._lock:
                self._transfer([self.INSTRUCTION_RTS_TX0])
            # A 125 kbit/s una trama clásica termina holgadamente antes de 20 ms.
            time.sleep(0.020)
            # Los registros de error valen cero durante una transmisión normal.
            # Aunque el conversor instalado no permite confiar en lecturas
            # complejas, cualquier valor persistente aquí permite detectar
            # ausencia de ACK, error pasivo o bus-off.
            return self.transmission_health()

        deadline = time.monotonic() + timeout
        while self.read_register(self.TXB0CTRL) & self.TXREQ:
            if time.monotonic() >= deadline:
                self._abort_transmission()
                raise CANTransmissionError("TXB0 seguía ocupado")
            time.sleep(0.001)

        self.write_register(self.TXB0CTRL, 0x00)
        self.write_registers(self.TXB0SIDH, header + bytes([len(data)]) + data)
        with self._lock:
            self._transfer([self.INSTRUCTION_RTS_TX0])

        while self.read_register(self.TXB0CTRL) & self.TXREQ:
            if time.monotonic() >= deadline:
                self._abort_transmission()
                raise CANTransmissionError("Timeout esperando la transmisión CAN")
            time.sleep(0.001)

        tx_ctrl = self.read_register(self.TXB0CTRL)
        eflg = self.read_register(self.EFLG)
        tec = self.read_register(self.TEC)
        rec = self.read_register(self.REC)
        self.bit_modify(self.CANINTF, 0x04, 0x00)  # limpiar TX0IF

        if tx_ctrl & self.TX_ERROR_MASK:
            raise CANTransmissionError(
                f"Error CAN: TXB0CTRL=0x{tx_ctrl:02X}, EFLG=0x{eflg:02X}, "
                f"TEC={tec}, REC={rec}"
            )
        return {"tx_ctrl": tx_ctrl, "eflg": eflg, "tec": tec, "rec": rec}

    def transmission_health(self) -> dict[str, int]:
        samples = []
        for _ in range(3):
            samples.append(
                {
                    "tx_ctrl": self.read_register(self.TXB0CTRL),
                    "eflg": self.read_register(self.EFLG),
                    "tec": self.read_register(self.TEC),
                    "rec": self.read_register(self.REC),
                }
            )
            time.sleep(0.001)
        # Para los bits sólo se conservan los que aparecen en las tres lecturas;
        # para los contadores se usa la mediana. Así una muestra aislada deformada
        # por el conversor de nivel no provoca una recuperación falsa.
        return {
            "tx_ctrl": samples[0]["tx_ctrl"]
            & samples[1]["tx_ctrl"]
            & samples[2]["tx_ctrl"],
            "eflg": samples[0]["eflg"]
            & samples[1]["eflg"]
            & samples[2]["eflg"],
            "tec": sorted(sample["tec"] for sample in samples)[1],
            "rec": sorted(sample["rec"] for sample in samples)[1],
        }

    def poll_receive(self) -> list[CANFrame]:
        if not self._readback:
            return []
        frames: list[CANFrame] = []
        flags = self.read_register(self.CANINTF)
        for flag, address in ((0x01, self.RXB0SIDH), (0x02, self.RXB1SIDH)):
            if not flags & flag:
                continue
            header = self.read_registers(address, 5)
            dlc = min(header[4] & 0x0F, 8)
            data = self.read_registers(address + 5, dlc)
            arbitration_id, is_extended = self._decode_id(header)
            frames.append(CANFrame(arbitration_id, data, is_extended))
            self.bit_modify(self.CANINTF, flag, 0x00)
        if self.read_register(self.EFLG) & 0xC0:
            self.bit_modify(self.EFLG, 0xC0, 0x00)
            self.bit_modify(self.CANINTF, 0x20, 0x00)
        return frames

    def diagnostics(self) -> dict[str, int]:
        if not self._readback:
            return {}
        return {
            "CANSTAT": self.read_register(self.CANSTAT),
            "CANCTRL": self.read_register(self.CANCTRL),
            "CNF1": self.read_register(self.CNF1),
            "CNF2": self.read_register(self.CNF2),
            "CNF3": self.read_register(self.CNF3),
            "EFLG": self.read_register(self.EFLG),
            "TEC": self.read_register(self.TEC),
            "REC": self.read_register(self.REC),
        }


def validate_start_values(voltage: float, current: float) -> None:
    if not MIN_START_VOLTAGE <= voltage <= MAX_START_VOLTAGE:
        raise ValueError(
            f"Tensión fuera de rango ({MIN_START_VOLTAGE:g}.."
            f"{MAX_START_VOLTAGE:g} V)"
        )
    if not MIN_START_CURRENT <= current <= MAX_START_CURRENT:
        raise ValueError(
            f"Corriente fuera de rango ({MIN_START_CURRENT:g}.."
            f"{MAX_START_CURRENT:g} A)"
        )
    if voltage * current > MAX_OUTPUT_POWER_W:
        raise ValueError(
            f"La consigna excede {MAX_OUTPUT_POWER_W / 1000:g} kW "
            f"({voltage * current / 1000:.2f} kW solicitados)"
        )


def build_control_payload(voltage: float, current: float, enabled: bool) -> bytes:
    if enabled:
        validate_start_values(voltage, current)
    else:
        voltage = 0.0
        current = 0.0

    voltage_bits = round(voltage * 10)
    current_bits = round(current * 100)
    return bytes([0xAA if enabled else 0x55, 0x00]) + struct.pack(
        "<HHBB", voltage_bits, current_bits, 0x00, 0x00
    )


def describe_status(frame: CANFrame) -> str | None:
    if (
        not frame.is_extended
        or frame.arbitration_id != STATUS_CAN_ID
        or len(frame.data) < 5
    ):
        return None
    state_byte = frame.data[0]
    state = {0x00: "OFF", 0x01: "ON"}.get(state_byte, f"FALLA(0x{state_byte:02X})")
    voltage = int.from_bytes(frame.data[1:3], "little") / 10.0
    current = int.from_bytes(frame.data[3:5], "little") / 100.0
    return f"PFC: {state}, V_out={voltage:.1f} V, I_out={current:.2f} A"


def print_diagnostics(mcp: MCP2515) -> None:
    values = mcp.diagnostics()
    if not values:
        print("MCP2515: modo TX directo, sin depender de MISO ni de INT")
        return
    print(
        "MCP2515: "
        + ", ".join(
            f"{name}={value}" if name in {"TEC", "REC"} else f"{name}=0x{value:02X}"
            for name, value in values.items()
        )
    )


class EventLog:
    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)

    def write(self, message: str, level: str = "INFO") -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
        print(line, flush=True)
        try:
            with open(self.path, "a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
        except OSError as exc:
            print(f"ADVERTENCIA: no se pudo escribir {self.path}: {exc}", file=sys.stderr)


def health_has_tx_error(health: dict[str, int]) -> bool:
    # Se ignoran RX0OVR/RX1OVR porque no impiden transmitir. El resto de EFLG,
    # TXERR/ABTF/MLOA o un contador TEC distinto de cero indican degradación.
    return bool(
        health.get("tx_ctrl", 0) & 0x70
        or health.get("eflg", 0) & 0x3F
        or health.get("tec", 0) > 0
    )


def format_health(health: dict[str, int]) -> str:
    return (
        f"TXB0CTRL=0x{health.get('tx_ctrl', 0):02X}, "
        f"EFLG=0x{health.get('eflg', 0):02X}, "
        f"TEC={health.get('tec', 0)}, REC={health.get('rec', 0)}"
    )


def send_stop_safely(mcp: MCP2515, count: int = 3) -> None:
    payload = build_control_payload(0.0, 0.0, False)
    for attempt in range(1, count + 1):
        result = mcp.send_extended(CONTROL_CAN_ID, payload)
        if result:
            print(
                f"STOP {attempt}/{count} transmitido: "
                f"TEC={result['tec']}, REC={result['rec']}, "
                f"EFLG=0x{result['eflg']:02X}"
            )
        else:
            print(f"STOP {attempt}/{count} transmitido (TX directo)")
        if attempt != count:
            deadline = time.monotonic() + SEND_PERIOD_SECONDS
            while time.monotonic() < deadline:
                for frame in mcp.poll_receive():
                    description = describe_status(frame)
                    if description:
                        print(description)
                time.sleep(0.02)


def run_interactive(
    mcp: MCP2515,
    event_log: EventLog,
    recovery_interval: float,
) -> None:
    voltage = 0.0
    current = 0.0
    enabled = False
    stop_requested = False
    frames_sent = 0
    recoveries = 0
    consecutive_tx_errors = 0
    last_health = {"tx_ctrl": 0, "eflg": 0, "tec": 0, "rec": 0}
    last_recovery = time.monotonic()

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    def recover(reason: str) -> None:
        nonlocal recoveries, consecutive_tx_errors, last_recovery, next_send
        event_log.write(f"RECUPERACIÓN CAN: {reason}", "WARN")
        mcp.initialize()
        recoveries += 1
        consecutive_tx_errors = 0
        last_recovery = time.monotonic()
        # Reaplicar inmediatamente la consigna vigente después del reset.
        payload = build_control_payload(voltage, current, enabled)
        mcp.send_extended(CONTROL_CAN_ID, payload)
        next_send = time.monotonic() + SEND_PERIOD_SECONDS
        event_log.write(
            f"CAN reinicializado; consigna reaplicada: "
            f"{'START' if enabled else 'STOP'}"
        )

    event_log.write("Control PFC iniciado")
    print("--- Control PFC TonHe por SPI/MCP2515 ---")
    print("Comandos: START,<tension>,<corriente> | STOP | STATUS | RECOVER | QUIT")
    print("Estado inicial seguro: STOP")

    next_send = 0.0
    while not stop_requested:
        now = time.monotonic()
        if (
            enabled
            and recovery_interval > 0
            and now - last_recovery >= recovery_interval
        ):
            try:
                recover(f"refresco preventivo cada {recovery_interval:g} s")
            except (MCP2515Error, OSError) as exc:
                event_log.write(f"Falló el refresco preventivo: {exc}", "ERROR")

        if now >= next_send:
            payload = build_control_payload(voltage, current, enabled)
            try:
                last_health = mcp.send_extended(CONTROL_CAN_ID, payload)
                frames_sent += 1
                if health_has_tx_error(last_health):
                    consecutive_tx_errors += 1
                    event_log.write(
                        f"Error CAN {consecutive_tx_errors}/"
                        f"{MAX_CONSECUTIVE_TX_ERRORS}: {format_health(last_health)}",
                        "WARN",
                    )
                    if consecutive_tx_errors >= MAX_CONSECUTIVE_TX_ERRORS:
                        recover("errores consecutivos de transmisión")
                else:
                    if consecutive_tx_errors:
                        event_log.write("La transmisión CAN volvió a estado normal")
                    consecutive_tx_errors = 0
            except (CANTransmissionError, MCP2515Error, OSError) as exc:
                event_log.write(f"Fallo SPI/CAN: {exc}", "ERROR")
                try:
                    recover("excepción de comunicación")
                except (MCP2515Error, OSError) as recovery_exc:
                    event_log.write(
                        f"No se pudo recuperar; se fuerza consigna STOP: {recovery_exc}",
                        "ERROR",
                    )
                    enabled = False
                    voltage = 0.0
                    current = 0.0
            except Exception as exc:
                enabled = False
                voltage = 0.0
                current = 0.0
                event_log.write(f"Error inesperado: {exc}; se fuerza STOP", "ERROR")
            next_send = now + SEND_PERIOD_SECONDS

        for frame in mcp.poll_receive():
            description = describe_status(frame)
            if description:
                print(description)

        timeout = max(0.0, min(0.050, next_send - time.monotonic()))
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if not readable:
            continue

        line = sys.stdin.readline()
        if not line:
            break
        command = line.strip().upper()
        if not command:
            continue

        try:
            parts = [part.strip() for part in command.split(",")]
            if parts[0] == "START" and len(parts) in {3, 4}:
                requested_voltage = float(parts[1])
                requested_current = float(parts[2])
                validate_start_values(requested_voltage, requested_current)
                voltage = requested_voltage
                current = requested_current
                enabled = True
                recover("nuevo comando START")
                event_log.write(f"START activo: {voltage:.1f} V, {current:.2f} A")
            elif parts[0] == "STOP":
                enabled = False
                voltage = 0.0
                current = 0.0
                recover("comando STOP")
                event_log.write("STOP activo")
            elif parts[0] == "STATUS":
                print(
                    f"Consigna: {'START' if enabled else 'STOP'}, "
                    f"{voltage:.1f} V, {current:.2f} A"
                )
                print(
                    f"Tramas: {frames_sent}, recuperaciones: {recoveries}, "
                    f"errores consecutivos: {consecutive_tx_errors}"
                )
                print(f"Último estado CAN: {format_health(last_health)}")
                if recovery_interval <= 0:
                    print("Refresco preventivo desactivado")
                elif not enabled:
                    print("Refresco preventivo en espera hasta el próximo START")
                else:
                    print(
                        f"Próximo refresco preventivo en "
                        f"{max(0.0, recovery_interval - (time.monotonic() - last_recovery)):.1f} s"
                    )
            elif parts[0] == "RECOVER":
                recover("solicitud manual")
            elif parts[0] in {"QUIT", "EXIT"}:
                break
            else:
                print(
                    "Comando inválido. Use START,311,10 | STOP | STATUS | "
                    "RECOVER | QUIT"
                )
        except ValueError as exc:
            print(f"Comando rechazado: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control del PFC TonHe usando HW-184 por SPI, sin línea INT"
    )
    parser.add_argument("--spi-bus", type=int, default=0)
    parser.add_argument("--spi-device", type=int, default=1)
    parser.add_argument("--spi-speed", type=int, default=500_000)
    parser.add_argument(
        "--recovery-interval",
        type=float,
        default=DEFAULT_RECOVERY_INTERVAL_SECONDS,
        help="segundos entre reinicios preventivos del MCP2515; 0 los desactiva",
    )
    parser.add_argument(
        "--log-file",
        default="~/Documents/control-pfc.log",
        help="archivo de eventos y recuperaciones",
    )
    parser.add_argument(
        "--readback",
        action="store_true",
        help="activa diagnóstico/recepción por MISO; requiere conversor SPI fiable",
    )
    parser.add_argument(
        "--stop-test",
        type=int,
        metavar="N",
        help="envía N tramas STOP y finaliza; no habilita la salida",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mcp: MCP2515 | None = None
    initialized = False
    lock_file = None
    try:
        if args.recovery_interval < 0:
            raise ValueError("--recovery-interval no puede ser negativo")

        # Impide que dos instancias manejen simultáneamente el mismo MCP2515.
        import fcntl

        lock_file = open("/tmp/control-pfc-spi.lock", "w", encoding="ascii")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MCP2515Error("Ya hay otra instancia controlando el PFC") from exc
        lock_file.write(str(os.getpid()))
        lock_file.flush()

        mcp = MCP2515(
            args.spi_bus,
            args.spi_device,
            args.spi_speed,
            readback=args.readback,
        )
        mcp.initialize()
        initialized = True
        print_diagnostics(mcp)

        if args.stop_test is not None:
            if args.stop_test < 1:
                raise ValueError("--stop-test debe ser al menos 1")
            send_stop_safely(mcp, args.stop_test)
        else:
            run_interactive(
                mcp,
                EventLog(args.log_file),
                args.recovery_interval,
            )
        return 0
    except (MCP2515Error, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if mcp is not None and initialized:
            try:
                send_stop_safely(mcp, 3)
            except Exception as exc:
                print(f"ADVERTENCIA: no se pudo confirmar STOP al salir: {exc}", file=sys.stderr)
        if mcp is not None:
            mcp.close()
        if lock_file is not None:
            lock_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
