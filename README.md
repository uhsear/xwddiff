# xwddiff

Classify the diff between two XML workspace documents as BREAKING, WIDENING or COSMETIC, and
exit non-zero on the class that matters.

A schema change is built and tested in a development geodatabase. Before the change goes to
production, somebody exports both schemas with Export XML Workspace Document and diffs the two
files. The diff is short. One field went from a short integer to a long integer, which is wider,
so no value can be lost. One text field went from NOT NULL to nullable, which accepts more, so
nothing can break. Both lines look minor, and the migration is scheduled.

The migration stops on the first of those two lines. Alter Field changes a field's type only when
the table is empty, and the production table holds rows. It changes nullability only when the
table is empty, too. Neither change is wrong. The geodatabase cannot make either change in place,
and a diff has no way to say so, because a diff lists what changed, not what the target can do.

This tool reads the same two files and puts every difference in one of three classes:

| Class | Meaning |
|---|---|
| `BREAKING` | The change cannot be applied in place to a dataset that holds rows, or existing rows or readers stop working. |
| `WIDENING` | The change applies in place, and existing rows and readers keep working. |
| `COSMETIC` | Only a name or description that people read changes. |

A difference that the tool has no rule for is `BREAKING`, never `COSMETIC`.

```
$ python xwddiff.py --self-test
xwddiff self-test: no geodatabase, no network, no arcpy
--------------------------------------------------------------------
PASS  a short integer widened to a long integer is BREAKING, because Alter Field changes a type only on an empty table  <-- pinned defect
PASS  and the length that moves with the type is not reported again
PASS  a NOT NULL field that becomes nullable is BREAKING, not WIDENING  <-- pinned defect
PASS  a nullable field that becomes NOT NULL is BREAKING
...
PASS  the fields of an index are not read as the table's fields  <-- pinned defect
...
PASS  two datasets with one unqualified name refuse the document  <-- pinned defect
PASS  a dataset with no name refuses the document
PASS  a field named twice in one table refuses the diff instead of keeping the last one  <-- pinned defect
...
PASS  a DOCTYPE is refused before any entity expands  <-- pinned defect
PASS  a DOCTYPE in a UTF-16 file is refused too  <-- pinned defect
...
PASS  a new extent, DSID, grid size, spatial index flag or metadata changes nothing  <-- pinned defect
PASS  a default of 1 typed xs:int instead of xs:short changes nothing
PASS  an attribute that is not xsi:type counts in a comparison
PASS  a dataset element with no rule that differs is BREAKING, never COSMETIC  <-- pinned defect
PASS  a changed relationship class is BREAKING as unclassified
...
PASS  a NOT NULL field added to an existing table is BREAKING  <-- pinned defect
PASS  a GlobalID field added is WIDENING, though it is NOT NULL
PASS  a text field lengthened is WIDENING
PASS  a text field shortened is BREAKING
...
PASS  an index renamed by another database's registration number changes nothing  <-- pinned defect
...
PASS  an ambiguous document exits 3, not the 1 of a breaking change
PASS  and a refused run writes nothing, even with --apply  <-- pinned defect
PASS  --out without --apply writes nothing at all  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is refused by the parser  <-- pinned defect
PASS  a prefix of --self-test is refused too
PASS  main with no argv reads the arguments after the program name
PASS  a failed run prints the count and every failed label, and exits 1
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  the fixture refuses a fragment that is not unique
--------------------------------------------------------------------
127 assertions, 0 failed
```

The full run prints all 127 assertions. The `...` lines above are where this block is cut.

## The existing alternative

[GDBStudio](https://github.com/ope-nz/GDBStudio) already reads XML workspace documents and diffs
two of them. It does a lot well. It matches coded values and subtypes by code, so a renamed value
is not a delete and an add. It has ignore options for the noise that changes on every export.
Its `arcpy migrate` command writes the arcpy calls that bring one schema to the other, and it
writes a `# TODO` for each change that arcpy cannot express in place. It also validates schemas
and draws diagrams. Use it if you want any of that.

Its `diff` command exits `0` when two documents are equivalent and `4` when they differ. It does
not put a severity on each change, and its exit code does not tell a lengthened text field from a
retyped one. That severity class, and an exit code per class, is the whole of this tool. Both
statements come from GDBStudio's README. I have not run GDBStudio.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network and no
database. It reads two files and writes nothing unless you ask.

The self-test prints 127 assertions on every interpreter it has run on: Windows Python 3.13.2,
Windows Python 3.9.25 and Ubuntu Python 3.12.3. All three runs used `-W error`.

```
git clone https://github.com/uhsear/xwddiff.git
```

## Usage

Export both schemas with Export XML Workspace Document, set to `SCHEMA_ONLY`. Then name the one
the target matches today first, and the one it should match next second.

```
python xwddiff.py --self-test
python xwddiff.py before.xml after.xml
python xwddiff.py before.xml after.xml --out changes.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `OLD` | none | The document the target geodatabase matches today. Required unless `--self-test`. |
| `NEW` | none | The document the target is meant to match next. Required unless `--self-test`. |
| `--out` | off | Write every change to this CSV. Needs `--apply`. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

The parser refuses abbreviations, so `--ap` is an error and not `--apply`.

## What it says

These runs use synthetic files. Every dataset, field and domain name in them is invented.

```
$ python xwddiff.py before.xml after.xml
xwddiff: before.xml -> after.xml
BREAKING  field Hydrants.STATUS: Type "esriFieldTypeSmallInteger" -> "esriFieldTypeInteger". Alter Field changes a field's type only on an empty table, even when the new type is wider.
BREAKING  field Inspections.RESULT: IsNullable "false" -> "true". This looks wider, but Alter Field changes nullability only on an empty table.
WIDENING  field Hydrants.NOTES: Length "50" -> "255". Alter Field can lengthen a text field on a table that holds rows.
WIDENING  field Hydrants.COLOR: added. Existing rows read null in it.
WIDENING  domain HydrantStatus code 3: added ("Abandoned"). Every existing value stays valid.
COSMETIC  dataset Hydrants: AliasName "Hydrants" -> "Fire hydrants". Only the label people read changes.
changes: 2 BREAKING, 3 WIDENING, 1 COSMETIC
VERDICT: BREAKING. Do not run this as an in-place migration of a dataset that holds rows.
```

That run exits 1. The same changes without the two lines from the story exit 4:

```
$ python xwddiff.py before.xml widen.xml
xwddiff: before.xml -> widen.xml
WIDENING  field Hydrants.NOTES: Length "50" -> "255". Alter Field can lengthen a text field on a table that holds rows.
WIDENING  field Hydrants.COLOR: added. Existing rows read null in it.
WIDENING  domain HydrantStatus code 3: added ("Abandoned"). Every existing value stays valid.
changes: 0 BREAKING, 3 WIDENING, 0 COSMETIC
VERDICT: WIDENING. Every change applies in place, and existing rows and readers keep working.
```

An enterprise geodatabase writes a dataset name as `database.owner.name`, and a file geodatabase
writes only the name. The tool removes the database and owner before it matches two datasets. So
a production export and a development export compare on the dataset itself:

```
$ python xwddiff.py before.xml cosmetic.xml
xwddiff: before.xml -> cosmetic.xml
COSMETIC  dataset Hydrants: AliasName "Hydrants" -> "Fire hydrants". Only the label people read changes.
changes: 0 BREAKING, 0 WIDENING, 1 COSMETIC
VERDICT: COSMETIC. Only names and descriptions people read changed.
```

When two datasets in one document have the same name after that step, the tool refuses the
document. It does not choose one of them:

```
$ python xwddiff.py before.xml twin.xml
xwddiff: before.xml -> twin.xml
REFUSED   two datasets are both named 'Hydrants' once the database and owner are removed. A name that stands for two things cannot be diffed, so nothing was compared.
VERDICT: REFUSED
```

The same refusal applies to a field named twice in one table, a domain named twice, a coded value
code used twice, and a subtype code used twice. A dictionary keeps the last of two equal keys,
which would hide the first one from the diff.

`--out changes.csv` alone prints `Check only. changes.csv was not written. Re-run with --apply.`
and writes nothing. With `--apply` it writes one row per change, `BREAKING` rows first, under the
header `severity,kind,path,detail`.

## The rules

Each rule below is exercised by at least one self-test assertion. The Esri behaviour each one
relies on is cited under [Sources](#sources).

**Datasets**

| Change | Class | Why |
|---|---|---|
| Removed | BREAKING | Every reader and writer of it stops. |
| Added | WIDENING | Nothing that exists today reads it. |
| Type, as in table to feature class | BREAKING | A dataset's type is set when it is created. |
| Moved to another feature dataset | BREAKING | Its catalog path changes. |
| `ShapeType`, `HasZ`, `HasM`, `FeatureType` | BREAKING | Esri sets these when the feature class is created [5]. xwddiff knows no tool that changes them in place. |
| Spatial reference: WKID, type, tolerance, resolution or origin | BREAKING | Define Projection only relabels the coordinate system [4]. A real change means Project into a new feature class. |
| `SubtypeFieldName` | BREAKING | Every row's subtype is now read from a different field. |
| `DefaultSubtypeCode` | WIDENING | Only rows written from now on are affected. |
| `AliasName`, `ModelName` | COSMETIC | Only the label changes. |

**Fields**

| Change | Class | Why |
|---|---|---|
| Removed | BREAKING | Its values are gone. |
| Added, nullable | WIDENING | Existing rows read null in it. |
| Added, NOT NULL | BREAKING | Add Field cannot add a non-nullable field to a non-empty table [2]. |
| Added, of type GlobalID | WIDENING | Add Global IDs adds this field, and its page does not restrict it to empty tables [3]. |
| Type, in any direction | BREAKING | Alter Field changes a type only on an empty table [1]. |
| Text or blob length increased | WIDENING | Alter Field can lengthen it on a table that holds rows [1]. |
| Text or blob length decreased | BREAKING | Alter Field shortens it only on an empty table [1]. |
| Length of any other type, alone | BREAKING | Alter Field changes the length of a text or blob field only [1]. |
| `IsNullable`, in either direction | BREAKING | Alter Field changes nullability only on an empty table [1]. |
| `Precision` or `Scale` | BREAKING | Alter Field has no precision or scale parameter [1]. |
| `Required` | BREAKING | Alter Field has no parameter that changes it [1]. |
| Domain assigned, or changed to another domain | BREAKING | Only the values within a domain are valid for its field [6]. |
| Domain taken off | WIDENING | The field accepts any value of its type again. |
| `DefaultValue` | WIDENING | Only rows written from now on are affected. |
| `AliasName`, `ModelName` | COSMETIC | Only the label changes. |

When the type changes, the length, precision and scale that move with it are not reported again.

**Indexes.** Indexes are matched by the fields they cover, not by name. An enterprise geodatabase
names some indexes after a registration number, so one index has a different name in each
database. A unique index that is added, or an index that becomes unique, is BREAKING. Every
existing row must already be unique on it, and Esri's Add Attribute Index page does not say what
happens when they are not [7]. Every other index change is WIDENING, because no stored value
changes.

**Subtypes.** A subtype code removed is BREAKING, because rows that hold it no longer belong to a
subtype. A code added is WIDENING. A new subtype name is COSMETIC. A domain or default value set
for a field inside one subtype follows the field rules above.

**Domains**

| Change | Class |
|---|---|
| Removed | BREAKING |
| Added | WIDENING |
| Coded value domain to range domain, or back | BREAKING |
| `FieldType` | BREAKING |
| Coded value removed | BREAKING |
| Coded value added | WIDENING |
| Coded value description changed | COSMETIC |
| Range minimum raised, or maximum lowered | BREAKING |
| Range minimum lowered, or maximum raised | WIDENING |
| `MergePolicy`, `SplitPolicy` | WIDENING |
| `Description`, `Owner` | COSMETIC |

A domain is compared once, from the workspace's list of domains. Each field also carries a copy of
its domain, and only the domain name is read from that copy.

**Everything else.** Any other element that differs, on a dataset, a field, a subtype or a domain,
is BREAKING with the words `xwddiff has no rule for this element`. A relationship class is compared
this way. So is any element that a newer ArcGIS release adds to the format.

**Ignored on purpose.** These dataset elements change between two exports of one schema, or are
compared somewhere else:

- `CatalogPath`, `Name` and `Children`: compared through the dataset key and the parent
- `DSID`, `Extent`, `Metadata`, `MetadataRetrieved`, `HasSpatialIndex`: change without a schema change
- `ConfigurationKeyword`, `Versioned`, `CanVersion`: storage and registration, not schema
- `HasGlobalID`, `GlobalIDFieldName`: compared through the GlobalID field itself
- `RelationshipClassNames`: compared through the relationship classes themselves
- A field's `GeometryDef`: its shape type and spatial reference are compared on the dataset, and
  its grid sizes change when a spatial index is rebuilt

When both spatial references have a WKID, the WKT text is not compared. One WKID can be written as
slightly different WKT text by different releases. Numbers compare by value, so `1e4` equals
`10000`. An empty element such as `<ModelName/>` equals an absent one.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Identical, or COSMETIC changes only. |
| 1 | At least one BREAKING change. |
| 2 | A document could not be read, or `--out` could not be written. |
| 3 | Refused. A name in a document stands for two things, so nothing was compared. |
| 4 | WIDENING is the worst change. |
| 64 | Usage error, including an abbreviated flag. |

Exit 4 is not 0 on purpose. A widening change still needs a migration step somebody runs. A job
that wants to pass widening changes can test for `1` alone.

Exit 2 is not argparse's usage code here. The parser exits 64 for a usage error, so 2 always means
that a file could not be read or written.

## Limits

- The tool assumes that each dataset holds rows. On an empty table, Alter Field can change a type,
  a length or nullability, so some BREAKING changes are possible in place there.
- It reads only what the XML says. It does not open a geodatabase, so it cannot count rows or
  find the values that a new domain or a shorter field would reject.
- Names match exactly after the database and owner are removed. A rename is reported as a removal
  and an addition, and so is a change of case only, such as `Shape` and `SHAPE`.
- An export from a file geodatabase and one from an enterprise geodatabase can differ in system
  fields, precision and scale for the same schema. Those differences are reported. Compare two
  exports from the same kind of geodatabase when you can.
- The GlobalID rule relies on the Add Global IDs page not restricting it to empty tables. The page
  does not say in words that existing rows receive values.
- Attribute rules, contingent values, topologies, networks and other controller datasets are not
  classified one by one. A difference in them is reported as BREAKING with no rule.
- Field order is not compared. Index uniqueness is compared, and index sort order is not.
- It reads the whole document into memory. Export with `SCHEMA_ONLY`. An export that includes data
  can be very large, and the data section is never read.
- It does not open the `.zip` or `.z` file that the export tool writes when the output name ends
  that way [8]. It names a ZIP file as one and exits 2. Extract the `.xml` first.
- It refuses any document that has a DOCTYPE. An XML workspace document does not need one, and a
  DTD is where entity-expansion attacks live [9]. The check runs in Expat before ElementTree
  parses anything, so a UTF-16 file is checked too.
- The rules come from Esri's documentation, not from runs against ArcGIS. No ArcGIS export was
  diffed during testing. The fixtures follow the element layout in Esri's technical paper [10].
- `if __name__ == "__main__":` carries `# pragma: no branch`. Its false side is an import, which the
  self-test run never does. Every other branch is covered: 100% branch coverage of `xwddiff.py`
  under `coverage run --branch xwddiff.py --self-test`.
- It writes nothing without `--apply`, and a refused run writes nothing at all. It never changes a
  geodatabase, and it opens no socket.

## Sources

1. Alter Field: a type and nullability change only on an empty table, a length can only increase
   on a table with records, and there is no precision, scale or required parameter.
   <https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/alter-field-properties.html>
2. Add Field: "A nonnullable field cannot be added to a nonempty geodatabase feature class or table."
   <https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/add-field.html>
3. Add Global IDs. <https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/add-global-ids.html>
4. Define Projection: "it does not modify any geometry".
   <https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/define-projection.html>
5. Feature class basics: the geometry type, the x,y tolerance and the resolution are defined when
   the feature class is created.
   <https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/feature-class-basics.html>
6. Introduction to attribute domains: "only the values within that domain are valid for the field."
   <https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/an-overview-of-attribute-domains.html>
7. Add Attribute Index: unique indexes are not supported in a file geodatabase, and the page does
   not describe existing duplicates.
   <https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/add-attribute-index.html>
8. Export XML Workspace Document: `.xml`, `.zip` or `.z` output, and the `SCHEMA_ONLY` option.
   <https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/export-xml-workspace-document.html>
9. Python, XML security: entity-expansion attacks and the Expat version note.
   <https://docs.python.org/3/library/xml.html>
10. Esri technical paper J-9620, XML Schema of the Geodatabase, January 2012: the `DataElement`,
    `Children`, `FieldArray`, `Index`, `Domain` and `Subtype` layout used by the fixtures.
    <https://content.esri.com/support/whitepapers/ao_/j9620_xml_schema_of_geodatabase.pdf>

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [svcdrift](https://github.com/uhsear/svcdrift) - diff a published feature service against the
  dataset it was published from, and exit non-zero rather than call two different schemas the same.
- [gdbxray](https://github.com/uhsear/gdbxray) - print the file geodatabase schema that ogrinfo
  does not show: subtypes, attribute rules and attachment linkage.
- [fcload](https://github.com/uhsear/fcload) - load a dataset into a geodatabase, refusing the
  imports that corrupt silently.
- [whobreaks](https://github.com/uhsear/whobreaks) - report every item that references the one you
  are about to delete.
