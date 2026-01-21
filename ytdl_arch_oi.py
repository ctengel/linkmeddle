"""Use ObjectIndex as a yt-dlp 'download-archive'"""

from obj_idx import clilib as oic


def oif2archive(oif: oic.File) -> str:
    """convert objectindex File object to a yt-dlp archive_id string
    
    Returns none if the file is not deemed complete and full
    """
    if not oif.object['completed']:
        return None
    if oif.info['partial']:
        return None
    return f"{oif.info['extra']['ytdl-extractor'].lower()} {oif.info['extra']['ytdl-id']}"


class ObjIdxDlArch:
    """Set-like class for using object index as a yt-dlp download-archive
    
    requires an ObjectIndex client object to fire it up
    acts like a set so it can be passed as a download_archive option

    there is no 'add' - the way to add to OI is a complete non-partial upload
    or a 'deleted completed' entry
    """
    objidx = None
    archive_set = set()

    def __init__(self, objidx: oic.ObjectIndex) -> None:
        self.objidx = objidx

    def download_archive_set(self, bucket=None, extractor=None, refresh=False) -> set:
        """Returns and caches a full archive set of a given extractor
        
        Bucket not implemented
        """
        assert extractor
        if self.archive_set and not refresh:
            return self.archive_set
        assert extractor
        assert not bucket
        self.archive_set = {oif2archive(x) for x
                            in self.objidx.search_file({'extra': f"ytdl-extractor={extractor}"})}
        self.archive_set.discard(None)
        return self.archive_set

    def in_download_archive(self, extractor: str, video_id: str) -> bool:
        """Returns true if we can find object in OI"""
        if self.archive_set:
            return f"{extractor.lower()} {video_id}" in self.archive_set
        archive_set = {oif2archive(x)
                       for x in self.objidx.search_file({'extra': f"ytdl-id={video_id}"})
                       if x.info['extra']['ytdl-extractor'].lower() == extractor.lower()}
        archive_set.discard(None)
        return bool(archive_set)

    def str_in_download_archive(self, archive_key: str) -> bool:
        """similar to `in_download_archive` but accepts an archive key string"""
        if self.archive_set:
            return archive_key in self.archive_set
        extractor, _, video_id = archive_key.partition(' ')
        return self.in_download_archive(extractor, video_id)

    def url_in_download_archive(self, url: str) -> bool:
        """looks for a URL in OI"""
        archive_set = {oif2archive(x) for x in self.objidx.search_file({'url': url})}
        archive_set.discard(None)
        return bool(archive_set)

    def dict_in_download_archive(self, info_dict: dict) -> bool:
        """given an info_dict look for it in OI
        
        suitable for overriding ytdl.in_download_archive
        """
        if self.archive_set:
            return f"{info_dict['extractor_key'].lower()} {info_dict['id']}" in self.archive_set
        return self.url_in_download_archive(info_dict['webpage_url'])

    def __contains__(self, item: str) -> bool:
        return self.str_in_download_archive(archive_key=item)
