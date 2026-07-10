CREATE INDEX idx_recent ON events(id) WHERE created_at > NOW();
