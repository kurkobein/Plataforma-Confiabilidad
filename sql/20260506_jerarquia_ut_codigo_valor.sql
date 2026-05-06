USE Mindcodb2;

SET SQL_SAFE_UPDATES = 0;

SET @has_codigo := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND COLUMN_NAME = 'codigo'
);
SET @sql := IF(@has_codigo = 0,
    'ALTER TABLE reliability_nodojerarquia ADD COLUMN codigo VARCHAR(50) NOT NULL DEFAULT '''' AFTER parent_id',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

UPDATE reliability_nodojerarquia
SET codigo = UPPER(REGEXP_REPLACE(nombre, '[^[:alnum:]]+', '-'))
WHERE (codigo IS NULL OR codigo = '');

SET @idx_nombre := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND INDEX_NAME = 'uq_nodojerarquia_empresa_parent_nombre'
);
SET @sql := IF(@idx_nombre > 0,
    'ALTER TABLE reliability_nodojerarquia DROP INDEX uq_nodojerarquia_empresa_parent_nombre',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_codigo := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_nodojerarquia'
      AND INDEX_NAME = 'uq_nodojerarquia_empresa_parent_codigo'
);
SET @sql := IF(@idx_codigo = 0,
    'ALTER TABLE reliability_nodojerarquia ADD UNIQUE KEY uq_nodojerarquia_empresa_parent_codigo (empresa_id, parent_id, codigo)',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_abreviatura := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_niveljerarquia'
      AND INDEX_NAME = 'uq_niveljerarquia_empresa_abreviatura'
);
SET @sql := IF(@idx_abreviatura > 0,
    'ALTER TABLE reliability_niveljerarquia DROP INDEX uq_niveljerarquia_empresa_abreviatura',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_abreviatura := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_niveljerarquia'
      AND COLUMN_NAME = 'abreviatura'
);
SET @sql := IF(@has_abreviatura > 0,
    'ALTER TABLE reliability_niveljerarquia DROP COLUMN abreviatura',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET SQL_SAFE_UPDATES = 1;
