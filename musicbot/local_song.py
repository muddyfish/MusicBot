import taglib
import os
import re


def get_field(song, field, default=""):
    return song.tags.get(field, [""])[0].strip() or default


def number_key(name):
    parts = re.findall("[^0-9]+|[0-9]+", name)
    L = []
    for part in parts:
        try:
            L.append(int(part))
        except ValueError:
            L.append(part)
    return L


def sort_songs(song_urls):
    songs = {}
    for song_file in song_urls:
        try:
            song = taglib.File(song_file.encode("utf-8"))
            sort_field = (get_field(song, "ALBUM"),
                          number_key(get_field(song, "DISCNUMBER")),
                          number_key(get_field(song, "TRACKNUMBER")))
            songs[song_file] = sort_field
        except OSError:
            songs[song_file] = ("",)
    return sorted(songs, key=songs.get)


def get_info(song_url):
    try:
        song = taglib.File(song_url.encode("utf-8"))
    except OSError:
        return "Unknown", 0
    name = get_field(song, "TITLE", os.path.split(song_url)[-1][:-4])
    length = song.length
    return name, length
