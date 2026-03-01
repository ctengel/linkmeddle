CREATE TABLE playliststats2 (
	modified_date DATETIME, 
	playlist_count INTEGER NOT NULL, 
	different BOOLEAN, 
	success BOOLEAN NOT NULL, 
	download_count INTEGER, 
	failed_count INTEGER, 
	input_params VARCHAR, 
	output_params VARCHAR, 
	timestamp DATETIME NOT NULL, 
	newest_item DATETIME, 
	interval INTEGER, 
	entries_hash BLOB NOT NULL, 
	sched_id INTEGER NOT NULL, 
	stat_id INTEGER NOT NULL, 
	PRIMARY KEY (stat_id), 
	FOREIGN KEY(sched_id) REFERENCES playlistsched (sched_id)
);
INSERT INTO playliststats2 (stat_id, modified_date, playlist_count, different, success, download_count, failed_count, input_params, output_params, timestamp, newest_item, interval, entries_hash, sched_id)
SELECT stat_id, modified_date, playlist_count, different, success, download_count, failed_count, input_params, output_params, timestamp, newest_item, interval, entries_hash, sched_id FROM playliststats;
DROP TABLE playliststats;
ALTER TABLE playliststats2 RENAME TO playliststats;
