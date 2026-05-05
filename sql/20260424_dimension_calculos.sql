ALTER TABLE reliability_dimension
  ADD COLUMN tipo_calculo VARCHAR(30) NULL,
  ADD COLUMN config_calculo JSON NULL;
