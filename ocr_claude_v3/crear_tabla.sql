-- crea la base y la tabla que usa el programa, se ejecuta una sola vez
-- el nombre de la base debe ser el mismo que sql_base_datos en el archivo env

IF DB_ID(N'FacturasOCR') IS NULL
    CREATE DATABASE FacturasOCR;
GO

USE FacturasOCR;
GO

-- tabla donde el programa inserta una fila por pagina
IF OBJECT_ID(N'dbo.SeguimientoOCR', N'U') IS NULL
CREATE TABLE dbo.SeguimientoOCR
(
    IdSeguimiento BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SeguimientoOCR PRIMARY KEY,
    ArchivoOrigen NVARCHAR(500) NOT NULL,
    NumPagina INT NOT NULL CONSTRAINT CK_SeguimientoOCR_NumPagina CHECK (NumPagina >= 1),
    FechaCarga DATETIME2(0) NOT NULL CONSTRAINT DF_SeguimientoOCR_FechaCarga DEFAULT SYSUTCDATETIME(),
    NombreOrdenante NVARCHAR(200) NULL,
    FechaTransferencia DATE NULL,
    INVOICE NVARCHAR(100) NULL,
    Total DECIMAL(18,2) NULL,
    NombreBeneficiario NVARCHAR(200) NULL,
    BancoBeneficiario NVARCHAR(200) NULL,
    Revisar BIT NOT NULL CONSTRAINT DF_SeguimientoOCR_Revisar DEFAULT 0,
    MotivoRevision NVARCHAR(1000) NULL,
    JsonCrudo NVARCHAR(MAX) NULL CONSTRAINT CK_SeguimientoOCR_Json CHECK (JsonCrudo IS NULL OR ISJSON(JsonCrudo) = 1),
    CONSTRAINT UQ_SeguimientoOCR_ArchivoPagina UNIQUE (ArchivoOrigen, NumPagina)
);
GO

-- indice para consultar rapido los registros que hay que revisar
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID(N'dbo.SeguimientoOCR') AND name = N'IX_SeguimientoOCR_Revisar')
    CREATE INDEX IX_SeguimientoOCR_Revisar ON dbo.SeguimientoOCR (Revisar, FechaCarga);
GO
