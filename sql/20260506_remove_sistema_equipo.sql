USE Mindcodb2;

SET SQL_SAFE_UPDATES = 0;

SET @fk_name := (
    SELECT CONSTRAINT_NAME
    FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_equipo'
      AND COLUMN_NAME = 'sistema_id'
      AND REFERENCED_TABLE_NAME IS NOT NULL
    LIMIT 1
);
SET @sql := IF(@fk_name IS NOT NULL,
    CONCAT('ALTER TABLE reliability_equipo DROP FOREIGN KEY ', @fk_name),
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_name := (
    SELECT INDEX_NAME
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_equipo'
      AND COLUMN_NAME = 'sistema_id'
      AND INDEX_NAME <> 'PRIMARY'
    LIMIT 1
);
SET @sql := IF(@idx_name IS NOT NULL,
    CONCAT('ALTER TABLE reliability_equipo DROP INDEX ', @idx_name),
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_sistema := (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'reliability_equipo'
      AND COLUMN_NAME = 'sistema_id'
);
SET @sql := IF(@has_sistema > 0,
    'ALTER TABLE reliability_equipo DROP COLUMN sistema_id',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

DROP TABLE IF EXISTS reliability_sistemas;

SET SQL_SAFE_UPDATES = 1;
