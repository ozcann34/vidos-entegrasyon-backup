-- Migration: Add user_id column to sync_logs table
-- Run this SQL in your PostgreSQL database

-- Add user_id column (nullable for backward compatibility with existing data)
ALTER TABLE sync_logs ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- Create foreign key constraint
ALTER TABLE sync_logs 
ADD CONSTRAINT fk_sync_logs_user_id 
FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

-- Create index for performance
CREATE INDEX IF NOT EXISTS ix_sync_logs_user_id ON sync_logs(user_id);

-- Optional: Update existing records to associate with a user if possible
-- You may need to adjust this based on your data
-- UPDATE sync_logs SET user_id = 1 WHERE user_id IS NULL; -- Example: set to admin user
