# trashy

a high level api for handling recycle bins across platforms

### Installation
```
uv add trashy
```

### Usage
```py
import trashy

# trashy provides a way to move files to the bin, list entries in bin, and restore files from bin.
# create instance of Bin
bin = trashy.RecycleBin()

# move a file to the bin
bin.recycle(["path/to/file.txt"])

# list entries
bin.entries()

# restore a file from the bin
entry = bin.entries()[0]
bin.restore([entry])
```

That's it. It doesn't get any simpler than that.

Entries are returned as a list of `TrashEntry` dataclasses, which contain the following attributes:

```py
TrashEntry(
  name='Screenshot from 2026-06-10 17-35-55.png',
  original_path='/home/nspc911/Pictures/Screenshots/Screenshot from 2026-06-10 17-35-55.png',  # this will be None if entry doesn't contain this info
  deleted_at=datetime.datetime(2026, 6, 28, 11, 31, 17),  # this can be None if entry doesn't contain this info
  size=13075  # shouldn't be None, but it is possible.
)
```

For now, only the major 3 OSes are supported (Windows, Linux, MacOS). If you want to add support for your OS, feel free to open a PR.

### Like what I do? Check out similar projects
- [multiarchive](https://github.com/NSPC911/multiarchive): a high level api for handling archives across platforms
