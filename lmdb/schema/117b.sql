CREATE TABLE playlistvid2 (
        vid_id VARCHAR NOT NULL, 
        playlist_id INTEGER NOT NULL,
        extractor_id VARCHAR NOT NULL, 
        PRIMARY KEY (vid_id, playlist_id, extractor_id), 
        FOREIGN KEY(playlist_id) REFERENCES playlistsum (playlist_id)
);
INSERT INTO playlistvid2 (vid_id, playlist_id, extractor_id)
SELECT vid_id, playlist_id, extractor_id FROM playlistvid;
DROP TABLE playlistvid;
ALTER TABLE playlistvid2 RENAME TO playlistvid;
