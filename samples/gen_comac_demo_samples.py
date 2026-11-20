"""生成商飞风格的飞机传感器演示数据。

所有飞机编号、传感器、参数和数值均为合成数据，不对应真实飞机或真实运行数据。
"""

from __future__ import annotations

import csv
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260925
OUTPUT_DIR = Path(__file__).resolve().parent / "comac_demo"
LENGTH = 96
AIRCRAFT = ("C919-SYN-001", "C919-SYN-002", "ARJ21-SYN-001")
PHASES = ("GROUND", "TAKEOFF", "CLIMB", "CRUISE", "DESCENT", "LANDING")
ENGINE_PARAMETERS = ("egt_c", "fuel_flow_kg_h", "oil_pressure_kpa")
HYDRAULIC_COLUMNS = (
    "system_pressure_kpa",
    "fluid_temperature_c",
    "pump_speed_rpm",
)


def phase_for(index: int) -> str:
    return PHASES[(index // 8) % len(PHASES)]


def base_value(parameter: str) -> float:
    return {
        "egt_c": 620.0,
        "fuel_flow_kg_h": 1450.0,
        "oil_pressure_kpa": 410.0,
        "system_pressure_kpa": 20500.0,
        "fluid_temperature_c": 58.0,
        "pump_speed_rpm": 3150.0,
    }[parameter]


def amplitude(parameter: str) -> float:
    return {
        "egt_c": 45.0,
        "fuel_flow_kg_h": 120.0,
        "oil_pressure_kpa": 18.0,
        "system_pressure_kpa": 800.0,
        "fluid_temperature_c": 7.0,
        "pump_speed_rpm": 180.0,
    }[parameter]


def normal_value(
    rng: random.Random,
    parameter: str,
    index: int,
    aircraft_index: int,
) -> float:
    base = base_value(parameter) + aircraft_index * 3.5
    amp = amplitude(parameter)
    phase_offset = (index // 8) * 0.4
    daily = amp * math.sin(2 * math.pi * index / 24 + phase_offset)
    operation = amp * 0.35 * math.sin(2 * math.pi * index / 48)
    noise = rng.gauss(0, amp * 0.035)
    return base + daily + operation + noise


def write_engine_readings(rng: random.Random) -> None:
    path = OUTPUT_DIR / "engine_readings.csv"
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "flight_id",
                "aircraft_id",
                "flight_phase",
                "sensor_id",
                "parameter_id",
                "value",
                "is_fault",
            ]
        )
        for aircraft_index, aircraft_id in enumerate(AIRCRAFT):
            flight_id = f"FLT-{260301 + aircraft_index}"
            for engine in ("engine_left", "engine_right"):
                for parameter in ENGINE_PARAMETERS:
                    values = [
                        normal_value(rng, parameter, index, aircraft_index)
                        for index in range(LENGTH)
                    ]
                    is_fault = False
                    if (
                        aircraft_id == "C919-SYN-001"
                        and engine == "engine_left"
                        and parameter == "egt_c"
                    ):
                        for index in range(58, 68):
                            values[index] += 180 + (index - 58) * 8
                        is_fault = True
                    if (
                        aircraft_id == "ARJ21-SYN-001"
                        and engine == "engine_right"
                        and parameter == "oil_pressure_kpa"
                    ):
                        for index in range(55, 75):
                            values[index] -= 70
                        is_fault = True
                    for index, value in enumerate(values):
                        writer.writerow(
                            [
                                (start + timedelta(hours=index)).strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),
                                flight_id,
                                aircraft_id,
                                phase_for(index),
                                engine,
                                parameter,
                                f"{value:.5f}",
                                1 if is_fault else 0,
                            ]
                        )


def write_hydraulic_readings(rng: random.Random) -> None:
    path = OUTPUT_DIR / "hydraulic_readings.csv"
    start = datetime(2026, 4, 10, tzinfo=timezone.utc)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "aircraft_id",
                "sensor_id",
                "flight_phase",
                *HYDRAULIC_COLUMNS,
            ]
        )
        for aircraft_index, aircraft_id in enumerate(AIRCRAFT):
            for sensor_id in ("hydraulic_A", "hydraulic_B"):
                values = {
                    parameter: [
                        normal_value(rng, parameter, index, aircraft_index)
                        for index in range(LENGTH)
                    ]
                    for parameter in HYDRAULIC_COLUMNS
                }
                if (
                    aircraft_id == "C919-SYN-002"
                    and sensor_id == "hydraulic_B"
                ):
                    for index in range(52, 72):
                        values["system_pressure_kpa"][index] -= 3500
                        values["fluid_temperature_c"][index] += 22
                for index in range(LENGTH):
                    writer.writerow(
                        [
                            (start + timedelta(hours=index)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            aircraft_id,
                            sensor_id,
                            phase_for(index),
                            *[
                                f"{values[parameter][index]:.5f}"
                                for parameter in HYDRAULIC_COLUMNS
                            ],
                        ]
                    )


def write_schema_files() -> None:
    sql_path = OUTPUT_DIR / "c919_sensor_schema.sql"
    sql_path.write_text(
        """-- 合成示例：飞机、架次和发动机传感器业务表结构
CREATE TABLE aircraft_sensor_catalog (
    aircraft_id VARCHAR(64) NOT NULL,
    sensor_id VARCHAR(64) NOT NULL,
    sensor_type VARCHAR(64),
    installation_position VARCHAR(128),
    calibration_date DATE,
    PRIMARY KEY (aircraft_id, sensor_id)
);

CREATE TABLE engine_parameter_event (
    event_id VARCHAR(64) NOT NULL,
    flight_id VARCHAR(64),
    aircraft_id VARCHAR(64) NOT NULL,
    sensor_id VARCHAR(64) NOT NULL,
    parameter_id VARCHAR(64) NOT NULL,
    event_start DATETIME,
    event_end DATETIME,
    event_level VARCHAR(32),
    description VARCHAR(512),
    PRIMARY KEY (event_id)
);
""",
        encoding="utf-8",
    )

    csv_path = OUTPUT_DIR / "hydraulic_schema_columns.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "table_name",
                "column_name",
                "data_type",
                "nullable",
                "description",
            ]
        )
        rows = [
            ("hydraulic_sensor_catalog", "aircraft_id", "STRING", "false", "飞机编号"),
            ("hydraulic_sensor_catalog", "sensor_id", "STRING", "false", "液压传感器编号"),
            ("hydraulic_sensor_catalog", "system_name", "STRING", "true", "液压系统名称"),
            ("hydraulic_sensor_catalog", "installation_position", "STRING", "true", "安装位置"),
            ("hydraulic_sensor_catalog", "rated_pressure_kpa", "DOUBLE", "true", "额定压力"),
        ]
        writer.writerows(rows)

    json_path = OUTPUT_DIR / "flight_phase_schema.json"
    json_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "schema": "flight_ops",
                        "table_name": "flight_phase_event",
                        "columns": [
                            {"name": "flight_id", "data_type": "string", "nullable": False},
                            {"name": "aircraft_id", "data_type": "string", "nullable": False},
                            {"name": "phase_code", "data_type": "string", "nullable": False},
                            {"name": "start_time", "data_type": "datetime", "nullable": False},
                            {"name": "end_time", "data_type": "datetime", "nullable": True},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    write_engine_readings(rng)
    write_hydraulic_readings(rng)
    write_schema_files()
    print(OUTPUT_DIR)


if __name__ == "__main__":
    main()
