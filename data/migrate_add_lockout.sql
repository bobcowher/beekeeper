-- Migration: Add account lockout columns
-- Run this if upgrading from a version without lockout support

-- Add failed_login_attempts column if it doesn't exist
ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER DEFAULT 0;

-- Add locked_until column if it doesn't exist
ALTER TABLE users ADD COLUMN locked_until TIMESTAMP;
