-- 合成示例：飞机、架次和发动机传感器业务表结构
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
