-- League database backups (league/backup.py) are uploaded to the private `swings` bucket under
-- `league-backups/` as SQLite files. The bucket's MIME allow-list (20260930000000_swings.sql) rejects them,
-- so allow exactly that one type. Idempotent; nothing else about the bucket changes.
update storage.buckets
   set allowed_mime_types = array_append(allowed_mime_types, 'application/vnd.sqlite3')
 where id = 'swings'
   and not ('application/vnd.sqlite3' = any(allowed_mime_types));
