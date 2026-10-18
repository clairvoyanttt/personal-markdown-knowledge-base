-- Initial schema from the approved design; collection bootstrapping only.
PRAGMA foreign_keys = ON;
CREATE TABLE CollectionMeta (
    singletonId INTEGER NOT NULL PRIMARY KEY CHECK (singletonId=1),
    collectionId TEXT NOT NULL CHECK(length(collectionId)=32 AND collectionId NOT GLOB '*[^0-9a-f]*'),
    schemaVersion INTEGER NOT NULL CHECK(schemaVersion=1),
    collectionRevision INTEGER NOT NULL CHECK(collectionRevision>=1),
    state TEXT NOT NULL CHECK(state IN ('PREPARED','READY'))
);
CREATE TABLE NoteKeys (
    noteId TEXT NOT NULL PRIMARY KEY COLLATE BINARY CHECK(length(noteId)=32 AND noteId NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK(state IN ('LIVE','DELETED','MISSING_RESERVED')),
    liveNoteId TEXT COLLATE BINARY,
    lastSavedName TEXT,
    UNIQUE(noteId,state),
    CHECK((state='LIVE' AND liveNoteId IS NOT NULL AND liveNoteId=noteId)
       OR (state IN ('DELETED','MISSING_RESERVED') AND liveNoteId IS NULL)),
    CHECK(state<>'DELETED' OR lastSavedName IS NOT NULL),
    FOREIGN KEY(liveNoteId) REFERENCES Notes(noteId)
        ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE CommitReceipt (
    commandId TEXT NOT NULL PRIMARY KEY CHECK(length(commandId)=32 AND commandId NOT GLOB '*[^0-9a-f]*'),
    operationId TEXT NOT NULL CHECK(length(operationId)=32 AND operationId NOT GLOB '*[^0-9a-f]*'),
    collectionId TEXT NOT NULL CHECK(length(collectionId)=32 AND collectionId NOT GLOB '*[^0-9a-f]*'),
    operationKind TEXT NOT NULL CHECK(operationKind IN ('SAVE','DELETE','RESTORE_INIT','RESTORE_NOTE_INIT')),
    noteId TEXT,
    beforeRevision INTEGER,
    afterRevision INTEGER,
    collectionRevision INTEGER NOT NULL CHECK(collectionRevision>=1),
    CHECK(noteId IS NULL OR (length(noteId)=32 AND noteId NOT GLOB '*[^0-9a-f]*')),
    CHECK(beforeRevision IS NULL OR beforeRevision>=1),
    CHECK(afterRevision IS NULL OR afterRevision>=1),
    CHECK((operationKind='SAVE' AND noteId IS NOT NULL AND afterRevision IS NOT NULL
              AND (beforeRevision IS NULL OR afterRevision=beforeRevision+1))
       OR (operationKind='DELETE' AND noteId IS NOT NULL AND beforeRevision IS NOT NULL AND afterRevision IS NULL)
       OR (operationKind='RESTORE_INIT' AND noteId IS NULL AND beforeRevision IS NULL AND afterRevision IS NULL)
       OR (operationKind='RESTORE_NOTE_INIT' AND noteId IS NOT NULL AND beforeRevision IS NULL AND afterRevision IS NOT NULL))
);
CREATE TABLE Notes (
    noteId TEXT NOT NULL PRIMARY KEY COLLATE BINARY CHECK(length(noteId)=32 AND noteId NOT GLOB '*[^0-9a-f]*'),
    keyState TEXT NOT NULL DEFAULT 'LIVE' CHECK(keyState='LIVE'),
    name TEXT NOT NULL,
    nameKey TEXT NOT NULL COLLATE BINARY UNIQUE,
    body TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>=1),
    lastCommitId TEXT NOT NULL CHECK(length(lastCommitId)=32 AND lastCommitId NOT GLOB '*[^0-9a-f]*'),
    UNIQUE(noteId,revision),
    FOREIGN KEY(noteId,keyState) REFERENCES NoteKeys(noteId,state)
        ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE NoteTags (
    noteId TEXT NOT NULL COLLATE BINARY,
    tag TEXT NOT NULL COLLATE BINARY,
    PRIMARY KEY(noteId,tag),
    FOREIGN KEY(noteId) REFERENCES Notes(noteId) ON UPDATE NO ACTION ON DELETE CASCADE
);
CREATE TABLE RefOccurrence (
    sourceNoteId TEXT NOT NULL COLLATE BINARY,
    sourceRevision INTEGER NOT NULL CHECK(sourceRevision>=1),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    targetCollectionId TEXT NOT NULL CHECK(length(targetCollectionId)=32 AND targetCollectionId NOT GLOB '*[^0-9a-f]*'),
    targetNoteId TEXT NOT NULL CHECK(length(targetNoteId)=32 AND targetNoteId NOT GLOB '*[^0-9a-f]*'),
    startUtf16 INTEGER NOT NULL CHECK(startUtf16>=0),
    lengthUtf16 INTEGER NOT NULL CHECK(lengthUtf16=74),
    displayHint TEXT,
    PRIMARY KEY(sourceNoteId,ordinal),
    UNIQUE(sourceNoteId,startUtf16),
    FOREIGN KEY(sourceNoteId,sourceRevision) REFERENCES Notes(noteId,revision)
        ON UPDATE NO ACTION ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IX_Ref_Target ON RefOccurrence(targetCollectionId,targetNoteId);
CREATE TRIGGER TK_NoDelete BEFORE DELETE ON NoteKeys
BEGIN SELECT RAISE(ABORT,'identity deletion forbidden'); END;
CREATE TRIGGER TK_NoIdChange BEFORE UPDATE OF noteId ON NoteKeys
WHEN NEW.noteId<>OLD.noteId
BEGIN SELECT RAISE(ABORT,'identity modification forbidden'); END;
CREATE TRIGGER TN_NoIdChange BEFORE UPDATE OF noteId ON Notes
WHEN NEW.noteId<>OLD.noteId
BEGIN SELECT RAISE(ABORT,'identity modification forbidden'); END;
CREATE TRIGGER TK_State BEFORE UPDATE OF state ON NoteKeys
WHEN NOT (NEW.state=OLD.state OR (OLD.state='LIVE' AND NEW.state='DELETED'))
BEGIN SELECT RAISE(ABORT,'identity reuse forbidden'); END;
