# Runtime data

`cities.sqlite3` is built from the GeoNames `cities500` dataset:

```bash
python scripts/build_geonames.py data/cities.sqlite3
```

GeoNames data is licensed under CC BY 4.0. Attribution: © GeoNames, available from
<https://www.geonames.org/> and <https://download.geonames.org/export/dump/>.

The generated database and application database are runtime artifacts and are not committed.
