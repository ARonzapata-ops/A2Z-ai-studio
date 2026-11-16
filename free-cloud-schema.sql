CREATE TABLE IF NOT EXISTS drafts (
 id TEXT NOT NULL,
 owner TEXT NOT NULL,
 task TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 status TEXT NOT NULL,
 output TEXT NOT NULL DEFAULT '',
 day TEXT NOT NULL,
 created TEXT NOT NULL,
 PRIMARY KEY(owner,id)
);
CREATE INDEX IF NOT EXISTS drafts_day ON drafts(day);
CREATE INDEX IF NOT EXISTS drafts_owner_day ON drafts(owner,day);
CREATE INDEX IF NOT EXISTS drafts_created ON drafts(created);
