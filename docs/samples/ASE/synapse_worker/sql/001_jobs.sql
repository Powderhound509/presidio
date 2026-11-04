-- Install with an authorized Entra database principal. Never run as the worker.
-- Re-runnable v1 migration; incompatible schema versions require a new migration.
SET XACT_ABORT ON;
GO
IF SCHEMA_ID(N'docjobs') IS NULL EXEC(N'CREATE SCHEMA docjobs AUTHORIZATION dbo');
GO
BEGIN TRANSACTION;
IF OBJECT_ID(N'docjobs.SchemaVersion') IS NULL
BEGIN
    CREATE TABLE docjobs.SchemaVersion (version int NOT NULL PRIMARY KEY);
    INSERT docjobs.SchemaVersion VALUES (1);
END;
IF NOT EXISTS (SELECT 1 FROM docjobs.SchemaVersion WHERE version = 1)
    OR EXISTS (SELECT 1 FROM docjobs.SchemaVersion WHERE version <> 1)
    THROW 51000, 'unsupported_schema_version', 1;
IF OBJECT_ID(N'docjobs.Jobs') IS NULL
BEGIN
    CREATE TABLE docjobs.Jobs (
        job_id uniqueidentifier NOT NULL CONSTRAINT PK_DocumentJobs PRIMARY KEY,
        owner_id nvarchar(128) NOT NULL,
        document_id uniqueidentifier NOT NULL,
        version_id uniqueidentifier NOT NULL,
        extension varchar(8) NOT NULL,
        input_etag varchar(36) NOT NULL,
        operation varchar(10) NOT NULL,
        language char(2) NOT NULL,
        policy varchar(32) NOT NULL,
        status varchar(32) NOT NULL,
        attempts int NOT NULL CONSTRAINT DF_DocumentAttempts DEFAULT 0,
        available_at datetime2 NOT NULL,
        claim_token uniqueidentifier NULL,
        lease_until datetime2 NULL,
        result_manifest varchar(160) NULL,
        error_code varchar(64) NULL,
        created_at datetime2 NOT NULL CONSTRAINT DF_DocumentCreated DEFAULT SYSUTCDATETIME(),
        updated_at datetime2 NOT NULL CONSTRAINT DF_DocumentUpdated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_DocumentJob UNIQUE
            (owner_id, document_id, version_id, operation, language, policy),
        CONSTRAINT CK_DocumentStatus CHECK (status IN
            ('queued','running','retry_wait','succeeded','succeeded_with_warnings',
             'failed','dead_letter'))
    );
END;
IF OBJECT_ID(N'docjobs.Outbox') IS NULL
BEGIN
    CREATE TABLE docjobs.Outbox (
        job_id uniqueidentifier NOT NULL CONSTRAINT PK_DocumentOutbox PRIMARY KEY
            REFERENCES docjobs.Jobs(job_id),
        available_at datetime2 NOT NULL,
        dispatch_token uniqueidentifier NULL,
        lease_until datetime2 NULL,
        sent_at datetime2 NULL
    );
END;
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID(N'docjobs.Outbox') AND name = N'IX_DocumentDispatch')
    CREATE INDEX IX_DocumentDispatch ON docjobs.Outbox(sent_at, available_at, lease_until);
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID(N'docjobs.Jobs') AND name = N'IX_DocumentRecovery')
    CREATE INDEX IX_DocumentRecovery ON docjobs.Jobs(status, lease_until, available_at);
COMMIT;
GO
CREATE OR ALTER PROCEDURE docjobs.Submit
    @document_id uniqueidentifier, @version_id uniqueidentifier,
    @extension varchar(8), @input_etag varchar(36), @operation varchar(10),
    @language varchar(16) = 'en', @policy varchar(32) = 'native-v1'
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @extension IS NULL OR @extension COLLATE Latin1_General_100_BIN2 NOT IN
        ('pdf','txt','docx','doc','xlsx','png','jpg','jpeg','tif','tiff')
        OR @operation IS NULL
        OR @operation COLLATE Latin1_General_100_BIN2 NOT IN ('annotate','redact','sanitize')
        OR @policy IS NULL OR @policy COLLATE Latin1_General_100_BIN2 <> 'native-v1'
        OR DATALENGTH(@policy) <> 9
        OR DATALENGTH(@extension) <> LEN(@extension)
        OR DATALENGTH(@operation) <> LEN(@operation)
        OR @language IS NULL OR DATALENGTH(@language) <> 2
        OR @language COLLATE Latin1_General_100_BIN2 LIKE '%[^a-z]%'
        OR @document_id IS NULL OR @version_id IS NULL OR @input_etag IS NULL
        OR LEN(@input_etag) NOT BETWEEN 3 AND 36
        THROW 51000, 'invalid_submission', 1;
    DECLARE @etag varchar(36) = @input_etag;
    IF LEFT(@etag, 1) = '"' AND RIGHT(@etag, 1) = '"'
        SET @etag = SUBSTRING(@etag, 2, LEN(@etag) - 2);
    IF LEFT(@etag, 2) COLLATE Latin1_General_100_BIN2 <> '0x'
        OR DATALENGTH(@etag) NOT BETWEEN 3 AND 34
        OR SUBSTRING(@etag, 3, 32) COLLATE Latin1_General_100_BIN2 LIKE '%[^0-9A-Fa-f]%'
        THROW 51000, 'invalid_etag', 1;
    SET @input_etag = '"' + @etag + '"';
    DECLARE @owner nvarchar(128) = USER_NAME(), @id uniqueidentifier;
    BEGIN TRANSACTION;
    SELECT @id = job_id FROM docjobs.Jobs WITH (UPDLOCK, HOLDLOCK)
        WHERE owner_id = @owner AND document_id = @document_id
        AND version_id = @version_id AND operation = @operation
        AND language = @language AND policy = @policy;
    IF @id IS NULL
    BEGIN
        SET @id = NEWID();
        INSERT docjobs.Jobs
            (job_id, owner_id, document_id, version_id, extension, input_etag,
             operation, language, policy, status, available_at)
        VALUES (@id, @owner, @document_id, @version_id, @extension, @input_etag,
             @operation, @language, @policy, 'queued', SYSUTCDATETIME());
        INSERT docjobs.Outbox (job_id, available_at) VALUES (@id, SYSUTCDATETIME());
    END
    ELSE IF EXISTS (SELECT 1 FROM docjobs.Jobs WHERE job_id = @id AND
                    (extension <> @extension OR input_etag <> @input_etag))
    BEGIN
        ROLLBACK;
        THROW 51000, 'input_version_conflict', 1;
    END;
    COMMIT;
    SELECT CONVERT(varchar(36), @id) AS job_id;
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Status @job_id uniqueidentifier
AS
BEGIN
    SET NOCOUNT ON;
    SELECT CONVERT(varchar(36), j.job_id) AS job_id, j.status, j.attempts,
        j.error_code, j.result_manifest, j.updated_at,
        CASE WHEN o.sent_at IS NULL THEN 'pending' ELSE 'sent' END AS dispatch_state
    FROM docjobs.Jobs j JOIN docjobs.Outbox o ON j.job_id = o.job_id
    WHERE j.job_id = @job_id AND j.owner_id = USER_NAME();
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Dispatch @lease_seconds int
AS
BEGIN
    SET NOCOUNT ON;
    IF @lease_seconds NOT BETWEEN 60 AND 600 THROW 51000, 'invalid_lease', 1;
    ;WITH candidate AS (
        SELECT TOP (1) * FROM docjobs.Outbox WITH (UPDLOCK, READPAST, READCOMMITTEDLOCK)
        WHERE sent_at IS NULL AND available_at <= SYSUTCDATETIME()
          AND (lease_until IS NULL OR lease_until <= SYSUTCDATETIME())
        ORDER BY available_at, job_id
    )
    UPDATE candidate SET dispatch_token = NEWID(),
        lease_until = DATEADD(second, @lease_seconds, SYSUTCDATETIME())
    OUTPUT CONVERT(varchar(36), inserted.job_id) AS job_id,
           CONVERT(varchar(36), inserted.dispatch_token) AS dispatch_token;
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Dispatched
    @job_id uniqueidentifier, @dispatch_token uniqueidentifier
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE docjobs.Outbox SET sent_at = SYSUTCDATETIME(),
        lease_until = NULL, dispatch_token = NULL
    WHERE job_id = @job_id AND dispatch_token = @dispatch_token
      AND lease_until > SYSUTCDATETIME();
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Claim
    @job_id uniqueidentifier, @lease_seconds int, @max_attempts int
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @lease_seconds NOT BETWEEN 60 AND 600 OR @max_attempts NOT BETWEEN 1 AND 10
        THROW 51000, 'invalid_claim_policy', 1;
    BEGIN TRANSACTION;
    UPDATE docjobs.Jobs WITH (UPDLOCK, ROWLOCK)
    SET status = 'dead_letter', claim_token = NULL, lease_until = NULL,
        error_code = 'attempt_limit', updated_at = SYSUTCDATETIME()
    WHERE job_id = @job_id AND attempts >= @max_attempts
      AND ((status IN ('queued','retry_wait') AND available_at <= SYSUTCDATETIME())
        OR (status = 'running' AND lease_until <= SYSUTCDATETIME()));
    UPDATE docjobs.Jobs WITH (UPDLOCK, ROWLOCK)
    SET status = 'running', claim_token = NEWID(),
        lease_until = DATEADD(second, @lease_seconds, SYSUTCDATETIME()),
        attempts = attempts + 1, updated_at = SYSUTCDATETIME(), error_code = NULL
    WHERE job_id = @job_id AND attempts < @max_attempts
      AND ((status IN ('queued','retry_wait') AND available_at <= SYSUTCDATETIME())
        OR (status = 'running' AND lease_until <= SYSUTCDATETIME()));
    DECLARE @claimed int = @@ROWCOUNT;
    SELECT CONVERT(varchar(36), job_id) AS job_id, status,
        CONVERT(varchar(36), claim_token) AS claim_token,
        CONVERT(varchar(36), document_id) AS document_id,
        CONVERT(varchar(36), version_id) AS version_id,
        extension, input_etag, operation, language, policy, attempts,
        CASE WHEN status = 'running' AND @claimed = 1 THEN 1 ELSE 0 END AS claimed
    FROM docjobs.Jobs WHERE job_id = @job_id;
    COMMIT;
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Renew
    @job_id uniqueidentifier, @claim_token uniqueidentifier, @lease_seconds int
AS
BEGIN
    SET NOCOUNT ON;
    IF @lease_seconds NOT BETWEEN 60 AND 600 THROW 51000, 'invalid_lease', 1;
    UPDATE docjobs.Jobs SET lease_until = DATEADD(second, @lease_seconds, SYSUTCDATETIME()),
        updated_at = SYSUTCDATETIME()
    WHERE job_id = @job_id AND claim_token = @claim_token AND status = 'running'
        AND lease_until > SYSUTCDATETIME();
    SELECT @@ROWCOUNT AS owned;
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Complete
    @job_id uniqueidentifier, @claim_token uniqueidentifier, @warnings bit
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE docjobs.Jobs
    SET status = CASE WHEN @warnings = 1 THEN 'succeeded_with_warnings' ELSE 'succeeded' END,
        result_manifest = CONVERT(varchar(36), @job_id) + '/' +
                          CONVERT(varchar(36), @claim_token) + '/manifest.json',
        lease_until = NULL, updated_at = SYSUTCDATETIME()
    WHERE job_id = @job_id AND claim_token = @claim_token AND status = 'running'
        AND lease_until > SYSUTCDATETIME();
    SELECT @@ROWCOUNT AS owned;
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Fail
    @job_id uniqueidentifier, @claim_token uniqueidentifier, @error_code varchar(64),
    @retryable bit, @max_attempts int
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @error_code IS NULL OR @error_code COLLATE Latin1_General_100_BIN2
        LIKE '%[^a-z0-9_]%' OR @max_attempts NOT BETWEEN 1 AND 10
        THROW 51000, 'invalid_failure', 1;
    BEGIN TRANSACTION;
    DECLARE @status varchar(32), @when datetime2;
    SELECT @status =
        CASE WHEN @retryable = 0 THEN 'failed'
             WHEN attempts >= @max_attempts THEN 'dead_letter' ELSE 'retry_wait' END,
        @when = DATEADD(second, 30 * attempts, SYSUTCDATETIME())
    FROM docjobs.Jobs WITH (UPDLOCK, ROWLOCK)
    WHERE job_id = @job_id AND claim_token = @claim_token AND status = 'running'
        AND lease_until > SYSUTCDATETIME();
    IF @status IS NOT NULL
    BEGIN
        UPDATE docjobs.Jobs SET status = @status, error_code = @error_code,
            available_at = @when, lease_until = NULL, claim_token = NULL,
            updated_at = SYSUTCDATETIME() WHERE job_id = @job_id;
        IF @status = 'retry_wait'
            UPDATE docjobs.Outbox SET sent_at = NULL, available_at = @when,
                lease_until = NULL, dispatch_token = NULL WHERE job_id = @job_id;
    END;
    COMMIT;
    SELECT @status AS status;
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Reconcile
AS
BEGIN
    SET NOCOUNT ON;
    -- Recover abandoned claims and stale dispatches even if a queue message was lost.
    -- At-least-once re-publication is safe; Claim arbitrates concurrent deliveries.
    UPDATE o SET sent_at = NULL, lease_until = NULL, dispatch_token = NULL,
                 available_at = SYSUTCDATETIME()
    FROM docjobs.Outbox o JOIN docjobs.Jobs j ON o.job_id = j.job_id
    WHERE o.sent_at < DATEADD(minute, -10, SYSUTCDATETIME())
      AND ((j.status IN ('queued','retry_wait') AND j.available_at <= SYSUTCDATETIME())
        OR (j.status = 'running' AND j.lease_until <= SYSUTCDATETIME()));
END;
GO
CREATE OR ALTER PROCEDURE docjobs.Redrive @job_id uniqueidentifier
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    BEGIN TRANSACTION;
    UPDATE docjobs.Jobs SET status = 'queued', attempts = 0, error_code = NULL,
        available_at = SYSUTCDATETIME(), claim_token = NULL, lease_until = NULL,
        updated_at = SYSUTCDATETIME()
    WHERE job_id = @job_id AND status IN ('failed', 'dead_letter');
    IF @@ROWCOUNT <> 1
    BEGIN
        ROLLBACK;
        THROW 51000, 'redrive_requires_failed_job', 1;
    END;
    UPDATE docjobs.Outbox SET sent_at = NULL, lease_until = NULL,
        dispatch_token = NULL, available_at = SYSUTCDATETIME() WHERE job_id = @job_id;
    COMMIT;
END;
GO
IF DATABASE_PRINCIPAL_ID(N'document_submitter') IS NULL CREATE ROLE document_submitter;
IF DATABASE_PRINCIPAL_ID(N'document_worker') IS NULL CREATE ROLE document_worker;
IF DATABASE_PRINCIPAL_ID(N'document_operator') IS NULL CREATE ROLE document_operator;
GRANT EXECUTE ON docjobs.Submit TO document_submitter;
GRANT EXECUTE ON docjobs.Status TO document_submitter;
GRANT EXECUTE ON docjobs.Dispatch TO document_worker;
GRANT EXECUTE ON docjobs.Dispatched TO document_worker;
GRANT EXECUTE ON docjobs.Claim TO document_worker;
GRANT EXECUTE ON docjobs.Renew TO document_worker;
GRANT EXECUTE ON docjobs.Complete TO document_worker;
GRANT EXECUTE ON docjobs.Fail TO document_worker;
GRANT EXECUTE ON docjobs.Reconcile TO document_worker;
GRANT EXECUTE ON docjobs.Redrive TO document_operator;
GO
