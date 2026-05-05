ALTER TABLE reliability_acacarga
    ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'Incompleto' AFTER origen;

UPDATE reliability_acacarga
SET status = 'Completo';
