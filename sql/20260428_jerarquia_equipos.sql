USE mindcodb;

SET SQL_SAFE_UPDATES = 0;

CREATE TABLE IF NOT EXISTS reliability_niveljerarquia (
    id BIGINT NOT NULL AUTO_INCREMENT,
    empresa_id BIGINT NOT NULL,
    nombre VARCHAR(100) NOT NULL,
    abreviatura VARCHAR(20) NOT NULL,
    orden INT UNSIGNED NOT NULL DEFAULT 0,
    activo TINYINT(1) NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    UNIQUE KEY uq_niveljerarquia_empresa_orden (empresa_id, orden),
    UNIQUE KEY uq_niveljerarquia_empresa_nombre (empresa_id, nombre),
    UNIQUE KEY uq_niveljerarquia_empresa_abreviatura (empresa_id, abreviatura),
    KEY idx_niveljerarquia_empresa_orden (empresa_id, orden),
    CONSTRAINT fk_niveljerarquia_empresa
        FOREIGN KEY (empresa_id) REFERENCES reliability_empresa(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS reliability_nodojerarquia (
    id BIGINT NOT NULL AUTO_INCREMENT,
    empresa_id BIGINT NOT NULL,
    nivel_id BIGINT NOT NULL,
    parent_id BIGINT NULL,
    nombre VARCHAR(200) NOT NULL,
    orden INT UNSIGNED NOT NULL DEFAULT 0,
    activo TINYINT(1) NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    UNIQUE KEY uq_nodojerarquia_empresa_parent_nombre (empresa_id, parent_id, nombre),
    KEY idx_nodojerarquia_empresa_parent (empresa_id, parent_id),
    KEY idx_nodojerarquia_empresa_nivel (empresa_id, nivel_id),
    CONSTRAINT fk_nodojerarquia_empresa
        FOREIGN KEY (empresa_id) REFERENCES reliability_empresa(id),
    CONSTRAINT fk_nodojerarquia_nivel
        FOREIGN KEY (nivel_id) REFERENCES reliability_niveljerarquia(id),
    CONSTRAINT fk_nodojerarquia_parent
        FOREIGN KEY (parent_id) REFERENCES reliability_nodojerarquia(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Ajuste si ya habías ejecutado una versión anterior con codigo/descripcion.
SET @has_codigo := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND COLUMN_NAME = 'codigo'
);
SET @sql := IF(@has_codigo > 0,
    'UPDATE reliability_nodojerarquia SET nombre = codigo WHERE id > 0 AND (nombre IS NULL OR nombre = '''')',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_codigo := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND INDEX_NAME = 'uq_nodojerarquia_empresa_parent_codigo'
);
SET @sql := IF(@idx_codigo > 0,
    'ALTER TABLE reliability_nodojerarquia DROP INDEX uq_nodojerarquia_empresa_parent_codigo',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_codigo := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND COLUMN_NAME = 'codigo'
);
SET @sql := IF(@has_codigo > 0,
    'ALTER TABLE reliability_nodojerarquia DROP COLUMN codigo',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_descripcion := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND COLUMN_NAME = 'descripcion'
);
SET @sql := IF(@has_descripcion > 0,
    'ALTER TABLE reliability_nodojerarquia DROP COLUMN descripcion',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_nombre := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND INDEX_NAME = 'uq_nodojerarquia_empresa_parent_nombre'
);
SET @sql := IF(@idx_nombre = 0,
    'ALTER TABLE reliability_nodojerarquia ADD UNIQUE KEY uq_nodojerarquia_empresa_parent_nombre (empresa_id, parent_id, nombre)',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_nodo := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_equipo'
      AND COLUMN_NAME = 'nodo_id'
);
SET @sql := IF(@has_nodo = 0,
    'ALTER TABLE reliability_equipo ADD COLUMN nodo_id BIGINT NULL AFTER sistema_id, MODIFY COLUMN sistema_id BIGINT NULL',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_equipo_nodo := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_equipo'
      AND INDEX_NAME = 'idx_equipo_nodo_id'
);
SET @sql := IF(@idx_equipo_nodo = 0,
    'ALTER TABLE reliability_equipo ADD KEY idx_equipo_nodo_id (nodo_id)',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @fk_equipo_nodo := (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_equipo'
      AND CONSTRAINT_NAME = 'fk_equipo_nodojerarquia'
);
SET @sql := IF(@fk_equipo_nodo = 0,
    'ALTER TABLE reliability_equipo ADD CONSTRAINT fk_equipo_nodojerarquia FOREIGN KEY (nodo_id) REFERENCES reliability_nodojerarquia(id)',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE OR REPLACE VIEW vw_equipo_jerarquia AS
WITH RECURSIVE rutas AS (
    SELECT
        n.id,
        n.parent_id,
        n.empresa_id,
        n.nivel_id,
        n.nombre,
        CAST(n.nombre AS CHAR(1000)) AS ut,
        1 AS profundidad
    FROM reliability_nodojerarquia n
    WHERE n.parent_id IS NULL

    UNION ALL

    SELECT
        h.id,
        h.parent_id,
        h.empresa_id,
        h.nivel_id,
        h.nombre,
        CONCAT(r.ut, '-', h.nombre) AS ut,
        r.profundidad + 1 AS profundidad
    FROM reliability_nodojerarquia h
    INNER JOIN rutas r ON r.id = h.parent_id
)
SELECT
    e.id AS equipo_id,
    e.tag_equipo,
    e.nombre_equipo,
    COALESCE(r.ut, e.ut) AS ut,
    REPLACE(COALESCE(r.ut, e.ut), '-', ' > ') AS ruta_nombre,
    r.profundidad,
    e.sistema_id,
    e.nodo_id,
    COALESCE(r.empresa_id, s.empresa_id) AS empresa_id
FROM reliability_equipo e
LEFT JOIN reliability_nodojerarquia n ON n.id = e.nodo_id
LEFT JOIN rutas r ON r.id = n.id
LEFT JOIN reliability_sistemas s ON s.id = e.sistema_id;

SET SQL_SAFE_UPDATES = 1;
