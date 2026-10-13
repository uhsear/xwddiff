#!/usr/bin/env python
"""Classify the diff between two XML workspace documents as BREAKING, WIDENING or COSMETIC.

A schema change that looks minor in a diff can be one the target geodatabase
cannot perform on a table that holds rows. A short integer that becomes a long
integer is wider in every sense a person reads, and Alter Field still changes a
field's type only on an empty table. A field that stops being NOT NULL accepts
more values, and Alter Field still changes nullability only on an empty table.
A plain diff lists both lines and says nothing about which one stops the
migration.

This tool reads two documents written by Export XML Workspace Document and
puts every difference in one of three classes:

    BREAKING  the change cannot be applied in place to a dataset that holds
              rows, or existing rows or readers stop working
    WIDENING  the change applies in place, and existing rows and readers keep
              working
    COSMETIC  only a name or description people read changes

A difference this tool has no rule for is BREAKING, never COSMETIC.

    python xwddiff.py --self-test
    python xwddiff.py before.xml after.xml
    python xwddiff.py before.xml after.xml --out changes.csv --apply

Nothing is written without --apply. Nothing in a geodatabase is ever changed.

Exit codes: 0 identical or COSMETIC only, 1 at least one BREAKING change, 2 a
file could not be read or written, 3 refused because a document is ambiguous,
4 WIDENING is the worst change, 64 usage error.
"""

from __future__ import print_function

import argparse
import csv
import io
import math
import os
import shutil
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from xml.parsers import expat

BREAKING = "BREAKING"
WIDENING = "WIDENING"
COSMETIC = "COSMETIC"
SEVERITIES = (BREAKING, WIDENING, COSMETIC)

EXIT_CLEAN = 0
EXIT_BREAKING = 1
EXIT_UNREADABLE = 2
EXIT_REFUSED = 3
EXIT_WIDENING = 4
EXIT_USAGE = 64

TEXT_TYPES = ("esriFieldTypeString", "esriFieldTypeBlob")
OUT_COLUMNS = ("severity", "kind", "path", "detail")

# The reason printed after each change. Every Esri behaviour named here is
# cited in the README.
R_DS_REMOVED = "Every reader and writer of it stops."
R_DS_ADDED = "Nothing that exists today reads it."
R_DS_TYPE = ("A dataset's type is set when it is created, and no tool "
             "converts one in place.")
R_MOVED = ("Its catalog path changes, so every reference to the old path "
           "breaks.")
R_FIXED = ("A feature class holds one kind of geometry, and xwddiff knows no "
           "tool that changes this in place.")
R_SR = ("The x,y resolution and tolerance are defined when the feature class "
        "is created. Define Projection only relabels the coordinate system "
        "without moving a coordinate, so a real change means Project into a "
        "new feature class.")
R_LABEL = "Only the label people read changes."
R_FIELD_REMOVED = ("Its values are gone, and every reader or writer that "
                   "names it fails.")
R_FIELD_ADDED = "Existing rows read null in it."
R_FIELD_ADDED_NOT_NULL = ("Add Field cannot add a non-nullable field to a "
                          "non-empty table.")
R_GLOBALID = ("Add Global IDs adds this field, and Esri's page for it does "
              "not restrict it to empty tables.")
R_TYPE = ("Alter Field changes a field's type only on an empty table, even "
          "when the new type is wider.")
R_LENGTH_UP = "Alter Field can lengthen a text field on a table that holds rows."
R_LENGTH_DOWN = "Alter Field shortens a text field only on an empty table."
R_LENGTH_OTHER = ("Alter Field changes the length of a text or blob field "
                  "only, and xwddiff has no rule for this one.")
R_NULL_TIGHTEN = ("Existing rows may hold nulls, and Alter Field changes "
                  "nullability only on an empty table.")
R_NULL_LOOSEN = ("This looks wider, but Alter Field changes nullability only "
                 "on an empty table.")
R_PRECISION = ("Alter Field has no precision or scale parameter, so the "
               "column has to be rebuilt.")
R_REQUIRED = "Alter Field has no parameter that changes it."
R_DEFAULT = "Only rows written from now on are affected."
R_DOMAIN_ON = ("Only the values within a domain are valid for its field, so "
               "rows holding any other value become invalid.")
R_DOMAIN_OFF = "The field accepts any value of its type again."
R_INDEX = "No stored value changes. Only query speed can."
R_INDEX_UNIQUE = ("Every existing row must already be unique on it, and "
                  "Esri's Add Attribute Index page does not say what happens "
                  "when they are not.")
R_SUBTYPE_REMOVED = "Rows that hold this code no longer belong to a subtype."
R_SUBTYPE_ADDED = "Existing rows keep their codes."
R_SUBTYPE_FIELD = "Every row's subtype is now read from a different field."
R_DOMAIN_REMOVED = ("Fields that named it lose their list of valid values, "
                    "and readers that show its descriptions see raw codes.")
R_DOMAIN_ADDED = "Nothing that exists today uses it."
R_DOMAIN_KIND = "Every value rule of the old domain is gone."
R_DOMAIN_FIELDTYPE = "Fields of the old type no longer match the domain."
R_POLICY = "Only values written by future splits and merges change."
R_CODE_REMOVED = "Rows that hold this code no longer hold a valid value."
R_CODE_ADDED = "Every existing value stays valid."
R_CODE_NAME = ("The stored code is unchanged. Only the description people "
               "read changes.")
R_RANGE_NARROW = "Rows outside the new range no longer hold a valid value."
R_RANGE_WIDE = "Every existing value stays valid."
R_UNCLASSIFIED = ("xwddiff has no rule for this element, so it is treated as "
                  "BREAKING until a person reads it.")

VERDICTS = {
    BREAKING: "VERDICT: BREAKING. Do not run this as an in-place migration "
              "of a dataset that holds rows.",
    WIDENING: "VERDICT: WIDENING. Every change applies in place, and "
              "existing rows and readers keep working.",
    COSMETIC: "VERDICT: COSMETIC. Only names and descriptions people read "
              "changed.",
    None: "VERDICT: IDENTICAL. No difference in anything xwddiff compares.",
}
EXIT_FOR = {BREAKING: EXIT_BREAKING, WIDENING: EXIT_WIDENING,
            COSMETIC: EXIT_CLEAN, None: EXIT_CLEAN}

XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"


class Unreadable(Exception):
    """The input is not an XML workspace document this tool will read."""


class Ambiguous(Exception):
    """One name stands for two things, so no diff of it can be trusted."""


class Change(object):
    __slots__ = ("severity", "kind", "path", "detail")

    def __init__(self, severity, kind, path, detail):
        self.severity = severity
        self.kind = kind
        self.path = path
        self.detail = detail

    def line(self):
        # Names come from the documents. A line break in one must not forge a
        # report line, so control characters print as escapes.
        return "%-8s  %s %s: %s" % (self.severity, self.kind,
                                    plain(self.path), plain(self.detail))


class Dataset(object):
    __slots__ = ("key", "elem", "parent")

    def __init__(self, key, elem, parent):
        self.key = key
        self.elem = elem
        self.parent = parent


class Workspace(object):
    __slots__ = ("datasets", "domains")

    def __init__(self, datasets, domains):
        self.datasets = datasets
        self.domains = domains


# ------------------------------------------------------------- element access

def local(tag):
    """A tag or attribute name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def xtype(elem):
    """The xsi:type of an element without its prefix, as in DEFeatureClass."""
    return elem.get(XSI_TYPE, "").rsplit(":", 1)[-1]


def kids(elem, name):
    """Direct children with this local name. Never descendants."""
    if elem is None:
        return []
    return [c for c in elem if local(c.tag) == name]


def kid(elem, name):
    """The one direct child with this name, or None. Two of them is ambiguous."""
    found = kids(elem, name)
    if len(found) > 1:
        raise Ambiguous("<%s> appears %d times under one <%s>"
                        % (name, len(found), local(elem.tag)))
    return found[0] if found else None


def text(elem, name):
    """The stripped text of one direct child, or '' when it is absent."""
    child = kid(elem, name)
    return "" if child is None else (child.text or "").strip()


def blank(elem):
    """True for an element that says nothing, such as <ModelName/>."""
    return not elem.attrib and not (elem.text or "").strip() and len(elem) == 0


def canon(elem):
    """A comparable form of an element. Prefixes and whitespace do not count."""
    attrs = []
    for key, value in elem.attrib.items():
        name = local(key)
        if name == "type":
            value = value.rsplit(":", 1)[-1]
        attrs.append((name, value))
    return (local(elem.tag), tuple(sorted(attrs)), (elem.text or "").strip(),
            tuple(canon(c) for c in elem if not blank(c)))


def canon_list(elems):
    return [canon(e) for e in elems if not blank(e)]


def unique_map(elems, keyfn, what, where):
    """Map key to element, refusing a key that two elements share."""
    out = {}
    for elem in elems:
        key = keyfn(elem)
        if key in out:
            raise Ambiguous("%s %r appears twice in %s" % (what, key, where))
        out[key] = elem
    return out


def merged_keys(old, new):
    """Keys of old in order, then the keys only new has, in order."""
    return list(old) + [k for k in new if k not in old]


def as_number(value):
    """The finite number a text holds, or None. nan and inf are not bounds."""
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def same_value(a, b):
    """Two leaf texts agree. 1e-005 and 1e-05 are one number."""
    if a == b:
        return True
    x, y = as_number(a), as_number(b)
    return x is not None and y is not None and x == y


def plain(value):
    """The text with control and line-separator characters as escapes."""
    return "".join(
        repr(c)[1:-1] if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
        for c in value)


def show(value):
    return '"%s"' % value if value else "(none)"


# ------------------------------------------------------------------- reading

def refuse_doctype(data):
    """Refuse a DTD before ElementTree parses anything.

    An XML workspace document never carries a DOCTYPE, and a DOCTYPE is where
    entity-expansion attacks live. Expat reads the encoding itself, so a UTF-16
    file is checked as carefully as a UTF-8 one.
    """
    parser = expat.ParserCreate()

    def stop(*_args):
        raise Unreadable("the document declares a DOCTYPE. An XML workspace "
                         "document never does, so it is refused unread.")

    parser.StartDoctypeDeclHandler = stop
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, LookupError) as exc:
        # LookupError: the declaration names an encoding Python does not know.
        raise Unreadable("not well-formed XML: %s" % exc)


def nothing_to_compare(old, new):
    """True when neither Workspace holds a dataset or a domain.

    Two empty documents would diff clean, but only because nothing was read.
    """
    return not (old.datasets or old.domains or new.datasets or new.domains)


def unqualified(name):
    """'proddb.gisowner.Hydrants' and 'Hydrants' are one dataset."""
    return name.rsplit(".", 1)[-1]


def load(data):
    """Read one XML workspace document from bytes into a Workspace."""
    if data[:4] == b"PK\x03\x04":
        raise Unreadable("this is a ZIP archive, which is how Export XML "
                         "Workspace Document compresses its output. Extract "
                         "the .xml inside it and diff that.")
    refuse_doctype(data)
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, LookupError) as exc:
        # Expat without namespaces accepts what ElementTree rejects, such as
        # xsi:type with no xmlns:xsi declared.
        raise Unreadable("not well-formed XML: %s" % exc)
    if local(root.tag) != "Workspace":
        raise Unreadable("the root element is <%s>, not <Workspace>"
                         % local(root.tag))
    definition = kid(root, "WorkspaceDefinition")
    if definition is None:
        raise Unreadable("there is no <WorkspaceDefinition>")

    domains = unique_map(kids(kid(definition, "Domains"), "Domain"),
                         lambda d: text(d, "DomainName"), "domain",
                         "the document")
    datasets = {}

    def walk(container, parent):
        for elem in kids(container, "DataElement"):
            name = text(elem, "Name")
            if not name:
                raise Ambiguous("a <DataElement> has no <Name>")
            key = unqualified(name)
            if key in datasets:
                raise Ambiguous("two datasets are both named %r once the "
                                "database and owner are removed" % key)
            datasets[key] = Dataset(key, elem, parent)
            walk(kid(elem, "Children"), key)

    walk(kid(definition, "DatasetDefinitions"), "")
    return Workspace(datasets, domains)


# -------------------------------------------------------------- the decision

def compare(path, kind, a, b, rules, ignored, out):
    """Compare the direct children of two elements, one rule per child name.

    A rule is a (severity, reason) pair for a leaf, or a function that reads
    the two parent elements itself. A leaf is compared on its value alone, so
    an xs:short default and an xs:int default of 1 agree. A child name with no
    rule that differs in any way is BREAKING: the tool never calls a
    difference it does not understand minor.
    """
    names = merged_keys([local(c.tag) for c in a], [local(c.tag) for c in b])
    for name in sorted(set(names), key=names.index):
        if name in ignored:
            continue
        rule = rules.get(name)
        if callable(rule):
            rule(path, kind, a, b, out)
            continue
        if rule is None:
            if canon_list(kids(a, name)) != canon_list(kids(b, name)):
                out.append(Change(BREAKING, kind, path,
                                  "<%s> changed. %s" % (name, R_UNCLASSIFIED)))
            continue
        old, new = text(a, name), text(b, name)
        if not same_value(old, new):
            out.append(Change(rule[0], kind, path, "%s %s -> %s. %s" % (
                name, show(old), show(new), rule[1])))


def same_type_leaf(name, severity, reason):
    """A field leaf whose change is reported only when the type did not change.

    A type change already reports BREAKING, and the length and precision that
    move with it would only repeat that.
    """
    def rule(path, kind, a, b, out):
        old, new = text(a, name), text(b, name)
        if old == new or text(a, "Type") != text(b, "Type"):
            return
        out.append(Change(severity, kind, path, "%s %s -> %s. %s"
                          % (name, show(old), show(new), reason)))
    return rule


def length_rule(path, kind, a, b, out):
    old, new = text(a, "Length"), text(b, "Length")
    if same_value(old, new) or text(a, "Type") != text(b, "Type"):
        return
    detail = "Length %s -> %s. " % (show(old), show(new))
    if (text(b, "Type") not in TEXT_TYPES or as_number(old) is None
            or as_number(new) is None):
        out.append(Change(BREAKING, kind, path, detail + R_LENGTH_OTHER))
    elif as_number(new) > as_number(old):
        out.append(Change(WIDENING, kind, path, detail + R_LENGTH_UP))
    else:
        out.append(Change(BREAKING, kind, path, detail + R_LENGTH_DOWN))


def nullable_rule(path, kind, a, b, out):
    old, new = text(a, "IsNullable"), text(b, "IsNullable")
    if old.lower() == new.lower():
        return
    why = R_NULL_LOOSEN if new.lower() == "true" else R_NULL_TIGHTEN
    out.append(Change(BREAKING, kind, path, "IsNullable %s -> %s. %s"
                      % (show(old), show(new), why)))


def domain_shift(path, kind, old, new, out):
    """A field, or a field inside one subtype, changes the domain it names."""
    if old == new:
        return
    detail = "domain %s -> %s. " % (show(old), show(new))
    if not new:
        out.append(Change(WIDENING, kind, path, detail + R_DOMAIN_OFF))
    else:
        out.append(Change(BREAKING, kind, path, detail + R_DOMAIN_ON))


def field_domain_rule(path, kind, a, b, out):
    # Only the name. The body is a copy of the workspace domain, which is
    # compared once, there, instead of once per field that uses it.
    domain_shift(path, kind, text(kid(a, "Domain"), "DomainName"),
                 text(kid(b, "Domain"), "DomainName"), out)


FIELD_RULES = {
    "Type": (BREAKING, R_TYPE),
    "Length": length_rule,
    "IsNullable": nullable_rule,
    "Precision": same_type_leaf("Precision", BREAKING, R_PRECISION),
    "Scale": same_type_leaf("Scale", BREAKING, R_PRECISION),
    "Required": (BREAKING, R_REQUIRED),
    "AliasName": (COSMETIC, R_LABEL),
    "ModelName": (COSMETIC, R_LABEL),
    "DefaultValue": (WIDENING, R_DEFAULT),
    "Domain": field_domain_rule,
}
# GeometryDef repeats the dataset's shape type and spatial reference, which
# are compared on the dataset, and its grid sizes move when a spatial index
# is rebuilt, which is not a schema change.
FIELD_IGNORED = frozenset(["Name", "GeometryDef"])


def field_map(dataset, where):
    # Direct children only: an <Index> carries its own <Fields> copy, and
    # reading those as the table's fields would invent duplicates.
    array = kid(kid(dataset, "Fields"), "FieldArray")
    return unique_map(kids(array, "Field"), lambda f: text(f, "Name"),
                      "field", where)


def fields_rule(path, kind, a, b, out):
    old, new = field_map(a, path), field_map(b, path)
    for name in merged_keys(old, new):
        where = "%s.%s" % (path, name)
        if name not in new:
            out.append(Change(BREAKING, "field", where,
                              "removed. " + R_FIELD_REMOVED))
        elif name not in old:
            added = new[name]
            if text(added, "Type") == "esriFieldTypeGlobalID":
                out.append(Change(WIDENING, "field", where,
                                  "added. " + R_GLOBALID))
            elif text(added, "IsNullable").lower() == "false":
                out.append(Change(BREAKING, "field", where,
                                  "added as NOT NULL. " + R_FIELD_ADDED_NOT_NULL))
            else:
                out.append(Change(WIDENING, "field", where,
                                  "added. " + R_FIELD_ADDED))
        else:
            compare(where, "field", old[name], new[name], FIELD_RULES,
                    FIELD_IGNORED, out)


def index_map(dataset):
    """Indexes keyed by the fields they cover, never by name.

    An enterprise geodatabase names its row id index after a registration
    number, so the same index has a different name in every database.
    """
    out = {}
    for index in kids(kid(kid(dataset, "Indexes"), "IndexArray"), "Index"):
        cols = tuple(text(f, "Name") for f in
                     kids(kid(kid(index, "Fields"), "FieldArray"), "Field"))
        unique = text(index, "IsUnique").lower() == "true"
        out[cols] = out.get(cols, False) or unique
    return out


def indexes_rule(path, kind, a, b, out):
    old, new = index_map(a), index_map(b)
    for cols in merged_keys(old, new):
        where = "%s index(%s)" % (path, ",".join(cols))
        if cols not in new:
            out.append(Change(WIDENING, "index", where, "removed. " + R_INDEX))
        elif cols not in old:
            if new[cols]:
                out.append(Change(BREAKING, "index", where,
                                  "added as UNIQUE. " + R_INDEX_UNIQUE))
            else:
                out.append(Change(WIDENING, "index", where,
                                  "added. " + R_INDEX))
        elif new[cols] and not old[cols]:
            out.append(Change(BREAKING, "index", where,
                              "now UNIQUE. " + R_INDEX_UNIQUE))
        elif old[cols] and not new[cols]:
            out.append(Change(WIDENING, "index", where,
                              "no longer UNIQUE. " + R_INDEX))


def info_domain_rule(path, kind, a, b, out):
    domain_shift(path, kind, text(a, "DomainName"), text(b, "DomainName"), out)


INFO_RULES = {"DomainName": info_domain_rule,
              "DefaultValue": (WIDENING, R_DEFAULT)}


def field_infos_rule(path, kind, a, b, out):
    def infos(subtype):
        return unique_map(kids(kid(subtype, "FieldInfos"), "SubtypeFieldInfo"),
                          lambda i: text(i, "FieldName"), "subtype field",
                          path)
    old, new = infos(a), infos(b)
    empty = ET.Element("SubtypeFieldInfo")
    for name in merged_keys(old, new):
        compare("%s.%s" % (path, name), kind, old.get(name, empty),
                new.get(name, empty), INFO_RULES, frozenset(["FieldName"]),
                out)


SUBTYPE_RULES = {"SubtypeName": (COSMETIC, R_LABEL),
                 "FieldInfos": field_infos_rule}


def subtypes_rule(path, kind, a, b, out):
    def subtypes(dataset):
        return unique_map(kids(kid(dataset, "Subtypes"), "Subtype"),
                          lambda s: text(s, "SubtypeCode"), "subtype code",
                          path)
    old, new = subtypes(a), subtypes(b)
    for code in merged_keys(old, new):
        where = "%s subtype %s" % (path, code)
        if code not in new:
            out.append(Change(BREAKING, "subtype", where,
                              "removed. " + R_SUBTYPE_REMOVED))
        elif code not in old:
            out.append(Change(WIDENING, "subtype", where,
                              "added. " + R_SUBTYPE_ADDED))
        else:
            compare(where, "subtype", old[code], new[code], SUBTYPE_RULES,
                    frozenset(["SubtypeCode"]), out)


def spatial_reference_rule(path, kind, a, b, out):
    old, new = kid(a, "SpatialReference"), kid(b, "SpatialReference")
    # compare() calls this only when one side has the element, so at most
    # one of the two is missing.
    if old is None or new is None:
        out.append(Change(BREAKING, kind, path, "spatial reference %s. %s"
                          % ("added" if old is None else "removed", R_SR)))
        return
    leaves = merged_keys([local(c.tag) for c in old],
                         [local(c.tag) for c in new])
    # A WKID on both sides decides the coordinate system. The WKT text for one
    # WKID is not spelled the same by every release, so it is read only when a
    # side has no WKID.
    if text(old, "WKID") and text(new, "WKID"):
        leaves = [n for n in leaves if n != "WKT"]
    moved = []
    if xtype(old) != xtype(new):
        moved.append("type %s -> %s" % (show(xtype(old)), show(xtype(new))))
    for name in leaves:
        if not same_value(text(old, name), text(new, name)):
            moved.append("%s %s -> %s" % (name, show(text(old, name)),
                                          show(text(new, name))))
    if moved:
        out.append(Change(BREAKING, kind, path, "spatial reference %s. %s"
                          % ("; ".join(moved), R_SR)))


DATASET_RULES = {
    "Fields": fields_rule,
    "Indexes": indexes_rule,
    "Subtypes": subtypes_rule,
    "SpatialReference": spatial_reference_rule,
    "DatasetType": (BREAKING, R_DS_TYPE),
    "FeatureType": (BREAKING, R_FIXED),
    "ShapeType": (BREAKING, R_FIXED),
    "HasZ": (BREAKING, R_FIXED),
    "HasM": (BREAKING, R_FIXED),
    "SubtypeFieldName": (BREAKING, R_SUBTYPE_FIELD),
    "DefaultSubtypeCode": (WIDENING, R_DEFAULT),
    "AliasName": (COSMETIC, R_LABEL),
    "ModelName": (COSMETIC, R_LABEL),
}
# Each of these differs between two exports with no schema change, or is
# compared somewhere else: the name through the key, the children as datasets
# of their own, the GlobalID through its field, the relationship class names
# through the relationship classes. The README lists them.
DATASET_IGNORED = frozenset([
    "Name", "CatalogPath", "DSID", "Extent", "Metadata", "MetadataRetrieved",
    "ConfigurationKeyword", "Versioned", "CanVersion", "HasSpatialIndex",
    "Children", "HasGlobalID", "GlobalIDFieldName", "RelationshipClassNames",
])


def diff_dataset(key, a, b, out):
    if a.parent != b.parent:
        out.append(Change(BREAKING, "dataset", key, "moved from feature "
                          "dataset %s -> %s. %s"
                          % (show(a.parent), show(b.parent), R_MOVED)))
    if xtype(a.elem) != xtype(b.elem):
        out.append(Change(BREAKING, "dataset", key, "type %s -> %s. %s"
                          % (show(xtype(a.elem)), show(xtype(b.elem)),
                             R_DS_TYPE)))
        return
    compare(key, "dataset", a.elem, b.elem, DATASET_RULES, DATASET_IGNORED,
            out)


def coded_values_rule(path, kind, a, b, out):
    def codes(domain):
        return unique_map(kids(kid(domain, "CodedValues"), "CodedValue"),
                          lambda c: text(c, "Code"), "code", path)
    old, new = codes(a), codes(b)
    for code in merged_keys(old, new):
        where = "%s code %s" % (path, code)
        if code not in new:
            out.append(Change(BREAKING, "domain", where, "removed (%s). %s"
                              % (show(text(old[code], "Name")),
                                 R_CODE_REMOVED)))
        elif code not in old:
            out.append(Change(WIDENING, "domain", where, "added (%s). %s"
                              % (show(text(new[code], "Name")), R_CODE_ADDED)))
        elif text(old[code], "Name") != text(new[code], "Name"):
            out.append(Change(COSMETIC, "domain", where,
                              "description %s -> %s. %s"
                              % (show(text(old[code], "Name")),
                                 show(text(new[code], "Name")), R_CODE_NAME)))


def range_rule(name, narrower):
    """MinValue narrows when it rises; MaxValue narrows when it falls."""
    def rule(path, kind, a, b, out):
        old, new = text(a, name), text(b, name)
        if same_value(old, new):
            return
        detail = "%s %s -> %s. " % (name, show(old), show(new))
        x, y = as_number(old), as_number(new)
        if x is None or y is None:
            out.append(Change(BREAKING, kind, path, detail + R_UNCLASSIFIED))
        elif narrower(x, y):
            out.append(Change(BREAKING, kind, path, detail + R_RANGE_NARROW))
        else:
            out.append(Change(WIDENING, kind, path, detail + R_RANGE_WIDE))
    return rule


DOMAIN_RULES = {
    "FieldType": (BREAKING, R_DOMAIN_FIELDTYPE),
    "Description": (COSMETIC, R_LABEL),
    "Owner": (COSMETIC, R_LABEL),
    "MergePolicy": (WIDENING, R_POLICY),
    "SplitPolicy": (WIDENING, R_POLICY),
    "CodedValues": coded_values_rule,
    "MinValue": range_rule("MinValue", lambda old, new: new > old),
    "MaxValue": range_rule("MaxValue", lambda old, new: new < old),
}


def diff_domain(path, a, b, out):
    if xtype(a) != xtype(b):
        out.append(Change(BREAKING, "domain", path, "type %s -> %s. %s"
                          % (show(xtype(a)), show(xtype(b)), R_DOMAIN_KIND)))
        return
    compare(path, "domain", a, b, DOMAIN_RULES, frozenset(["DomainName"]), out)


def diff(old, new):
    """Every classified change from one Workspace to another, in a fixed order."""
    out = []

    def order(keys):
        return sorted(keys, key=lambda k: (k.lower(), k))

    for key in order(set(old.datasets) | set(new.datasets)):
        a, b = old.datasets.get(key), new.datasets.get(key)
        if b is None:
            out.append(Change(BREAKING, "dataset", key,
                              "removed. " + R_DS_REMOVED))
        elif a is None:
            out.append(Change(WIDENING, "dataset", key, "added (%s). %s"
                              % (xtype(b.elem), R_DS_ADDED)))
        else:
            diff_dataset(key, a, b, out)
    for name in order(set(old.domains) | set(new.domains)):
        a, b = old.domains.get(name), new.domains.get(name)
        if b is None:
            out.append(Change(BREAKING, "domain", name,
                              "removed. " + R_DOMAIN_REMOVED))
        elif a is None:
            out.append(Change(WIDENING, "domain", name,
                              "added. " + R_DOMAIN_ADDED))
        else:
            diff_domain(name, a, b, out)
    return out


def worst(changes):
    """The most severe class present, or None when nothing differs."""
    for severity in SEVERITIES:
        if any(c.severity == severity for c in changes):
            return severity
    return None


def describe(changes):
    """The report lines, BREAKING first, ending in the verdict."""
    lines = []
    for severity in SEVERITIES:
        lines.extend(c.line() for c in changes if c.severity == severity)
    lines.append("changes: %s" % ", ".join(
        "%d %s" % (sum(1 for c in changes if c.severity == s), s)
        for s in SEVERITIES))
    lines.append(VERDICTS[worst(changes)])
    return lines


# --------------------------------------------------------------- the fixture

def _field(name, ftype, length="0", nullable="true", domain="", alias=None,
           extra=""):
    """One <Field> as Export XML Workspace Document writes it. Test data only."""
    dom = ""
    if domain:
        dom = ("<Domain xsi:type='esri:CodedValueDomain'><DomainName>%s"
               "</DomainName></Domain>" % domain)
    return ("<Field xsi:type='esri:Field'><Name>%s</Name><Type>%s</Type>"
            "<IsNullable>%s</IsNullable><Length>%s</Length>"
            "<Precision>0</Precision><Scale>0</Scale>%s"
            "<AliasName>%s</AliasName><ModelName>%s</ModelName>%s</Field>"
            % (name, ftype, nullable, length, extra,
               name if alias is None else alias, name, dom))


SR_FT = ("<SpatialReference xsi:type='esri:ProjectedCoordinateSystem'>"
         "<WKT>PROJCS[\"Synthetic_Grid_Feet\"]</WKT><XOrigin>-1000</XOrigin>"
         "<YOrigin>-1000</YOrigin><XYScale>10000</XYScale>"
         "<XYTolerance>0.0032808333333333331</XYTolerance>"
         "<WKID>2236</WKID><LatestWKID>2236</LatestWKID></SpatialReference>")

F_OID = _field("OBJECTID", "esriFieldTypeOID", "4", "false",
               extra="<Required>true</Required>")
F_SHAPE = _field("Shape", "esriFieldTypeGeometry", extra=(
    "<Required>true</Required><GeometryDef xsi:type='esri:GeometryDef'>"
    "<GeometryType>esriGeometryPoint</GeometryType><HasM>false</HasM>"
    "<HasZ>false</HasZ>" + SR_FT + "<GridSize0>250</GridSize0>"
    "</GeometryDef>"))
F_FACILITYID = _field("FACILITYID", "esriFieldTypeString", "20")
F_STATUS = _field("STATUS", "esriFieldTypeSmallInteger", "2",
                  domain="HydrantStatus")
F_PRESSURE = _field("PRESSURE", "esriFieldTypeInteger", "4",
                    domain="PressureRange")
F_KIND = _field("KIND", "esriFieldTypeSmallInteger", "2")
F_NOTES = _field("NOTES", "esriFieldTypeString", "50", alias="Notes")
F_HYDRANTID = _field("HYDRANTID", "esriFieldTypeString", "20")
F_RESULT = _field("RESULT", "esriFieldTypeString", "10", "false")
F_INSPECTED = _field("INSPECTED", "esriFieldTypeDate", "8")

IDX_OID = ("<Index xsi:type='esri:Index'><Name>R7_SDE_ROWID_UK</Name>"
           "<IsUnique>true</IsUnique><IsAscending>true</IsAscending>"
           "<Fields xsi:type='esri:Fields'><FieldArray>"
           "<Field xsi:type='esri:Field'><Name>OBJECTID</Name>"
           "<Type>esriFieldTypeOID</Type></Field></FieldArray></Fields>"
           "</Index>")
# The index copy of FACILITYID says esriFieldTypeInteger on purpose. If the
# reader ever took index fields as table fields, the type would change and
# the name would repeat, and both are pinned.
IDX_FACILITY = ("<Index xsi:type='esri:Index'><Name>IDX_FACILITYID</Name>"
                "<IsUnique>false</IsUnique><IsAscending>true</IsAscending>"
                "<Fields xsi:type='esri:Fields'><FieldArray>"
                "<Field xsi:type='esri:Field'><Name>FACILITYID</Name>"
                "<Type>esriFieldTypeInteger</Type></Field></FieldArray>"
                "</Fields></Index>")

SUB_WET = ("<Subtype xsi:type='esri:Subtype'><SubtypeName>Wet barrel"
           "</SubtypeName><SubtypeCode>1</SubtypeCode><FieldInfos>"
           "<SubtypeFieldInfo><FieldName>STATUS</FieldName><DomainName>"
           "HydrantStatus</DomainName><DefaultValue xsi:type='xs:short'>1"
           "</DefaultValue></SubtypeFieldInfo></FieldInfos></Subtype>")
SUB_DRY = ("<Subtype xsi:type='esri:Subtype'><SubtypeName>Dry barrel"
           "</SubtypeName><SubtypeCode>2</SubtypeCode><FieldInfos>"
           "<SubtypeFieldInfo><FieldName>STATUS</FieldName><DomainName>"
           "HydrantStatus</DomainName></SubtypeFieldInfo></FieldInfos>"
           "</Subtype>")

DOM_STATUS = ("<Domain xsi:type='esri:CodedValueDomain'><DomainName>"
              "HydrantStatus</DomainName><FieldType>esriFieldTypeSmallInteger"
              "</FieldType><MergePolicy>esriMPTDefaultValue</MergePolicy>"
              "<SplitPolicy>esriSPTDefaultValue</SplitPolicy><Description>"
              "Hydrant status</Description><Owner/><CodedValues>"
              "<CodedValue><Name>In service</Name><Code xsi:type='xs:short'>1"
              "</Code></CodedValue><CodedValue><Name>Out of service</Name>"
              "<Code xsi:type='xs:short'>2</Code></CodedValue></CodedValues>"
              "</Domain>")
DOM_PRESSURE = ("<Domain xsi:type='esri:RangeDomain'><DomainName>"
                "PressureRange</DomainName><FieldType>esriFieldTypeInteger"
                "</FieldType><MergePolicy>esriMPTDefaultValue</MergePolicy>"
                "<SplitPolicy>esriSPTDefaultValue</SplitPolicy><Description>"
                "Static pressure, psi</Description><Owner/>"
                "<MaxValue xsi:type='xs:int'>200</MaxValue>"
                "<MinValue xsi:type='xs:int'>0</MinValue></Domain>")


def sample_document():
    """A small synthetic XML workspace document. Every name in it is invented."""
    hydrants = (
        "<DataElement xsi:type='esri:DEFeatureClass'>"
        "<CatalogPath>/FD=Water/FC=Hydrants</CatalogPath><Name>Hydrants</Name>"
        "<DatasetType>esriDTFeatureClass</DatasetType><DSID>4</DSID>"
        "<Versioned>false</Versioned><CanVersion>false</CanVersion>"
        "<ConfigurationKeyword/><HasOID>true</HasOID>"
        "<OIDFieldName>OBJECTID</OIDFieldName>"
        "<Fields xsi:type='esri:Fields'><FieldArray xsi:type='esri:ArrayOfField'>"
        + F_OID + F_SHAPE + F_FACILITYID + F_STATUS + F_PRESSURE + F_KIND
        + F_NOTES + "</FieldArray></Fields>"
        "<Indexes xsi:type='esri:Indexes'><IndexArray>" + IDX_OID
        + IDX_FACILITY + "</IndexArray></Indexes>"
        "<AliasName>Hydrants</AliasName><ModelName/>"
        "<HasGlobalID>false</HasGlobalID><GlobalIDFieldName/>"
        "<ExtensionProperties xsi:type='esri:PropertySet'><PropertyArray "
        "xsi:type='esri:ArrayOfPropertySetProperty'/></ExtensionProperties>"
        "<SubtypeFieldName>KIND</SubtypeFieldName>"
        "<DefaultSubtypeCode>1</DefaultSubtypeCode>"
        "<Subtypes xsi:type='esri:ArrayOfSubtype'>" + SUB_WET + SUB_DRY
        + "</Subtypes>"
        "<FeatureType>esriFTSimple</FeatureType>"
        "<ShapeType>esriGeometryPoint</ShapeType>"
        "<ShapeFieldName>Shape</ShapeFieldName><HasM>false</HasM>"
        "<HasZ>false</HasZ><HasSpatialIndex>true</HasSpatialIndex>"
        "<Extent xsi:type='esri:EnvelopeN'><XMin>10</XMin><YMin>10</YMin>"
        "<XMax>90</XMax><YMax>90</YMax></Extent>" + SR_FT
        + "</DataElement>")
    inspections = (
        "<DataElement xsi:type='esri:DETable'>"
        "<CatalogPath>/OC=Inspections</CatalogPath><Name>Inspections</Name>"
        "<DatasetType>esriDTTable</DatasetType><DSID>5</DSID>"
        "<Fields xsi:type='esri:Fields'><FieldArray>" + F_OID + F_HYDRANTID
        + F_RESULT + F_INSPECTED + "</FieldArray></Fields>"
        "<AliasName>Inspections</AliasName></DataElement>")
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<esri:Workspace xmlns:esri='http://www.esri.com/schemas/ArcGIS/10.8' "
        "xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' "
        "xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
        "<WorkspaceDefinition xsi:type='esri:WorkspaceDefinition'>"
        "<WorkspaceType>esriLocalDatabaseWorkspace</WorkspaceType><Version/>"
        "<Domains xsi:type='esri:ArrayOfDomain'>" + DOM_STATUS + DOM_PRESSURE
        + "</Domains>"
        "<DatasetDefinitions xsi:type='esri:ArrayOfDataElement'>"
        "<DataElement xsi:type='esri:DEFeatureDataset'>"
        "<CatalogPath>/FD=Water</CatalogPath><Name>Water</Name>"
        "<Children xsi:type='esri:ArrayOfDataElement'>" + hydrants
        + "</Children>" + SR_FT + "</DataElement>" + inspections
        + "</DatasetDefinitions></WorkspaceDefinition>"
        "<WorkspaceData xsi:type='esri:WorkspaceData'/></esri:Workspace>\n")


def swap(doc, old, new):
    """Replace a fragment that must occur exactly once. Test data only."""
    if doc.count(old) != 1:
        raise AssertionError("fixture fragment occurs %d times: %s"
                             % (doc.count(old), old[:60]))
    return doc.replace(old, new)


# ------------------------------------------------------------------ self-test

def self_test():
    """Run the assertions in a temporary directory that is always removed."""
    tmp = tempfile.mkdtemp(prefix="xwddiff-")
    try:
        return _assertions(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _assertions(tmp):
    """Assertions over the decision core and the CLI. No network, no arcpy.

    tmp is the only place they write, and self_test() removes it.
    """
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, exc_type, label, words=()):
        """fn raises exc_type, and the message holds every one of words."""
        try:
            fn()
        except exc_type as exc:
            check(all(w in str(exc) for w in words), label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    base = sample_document()

    def changes(new_doc, old_doc=None):
        return diff(load((old_doc or base).encode("utf-8")),
                    load(new_doc.encode("utf-8")))

    def only(new_doc, severity, *words):
        """Exactly one change, of this severity, whose line holds every word."""
        found = changes(new_doc)
        return (len(found) == 1 and found[0].severity == severity
                and all(w in found[0].line() for w in words))

    def field_swap(old_field, **kw):
        return swap(base, old_field, _field(**kw))

    print("xwddiff self-test: no geodatabase, no network, no arcpy")
    print("-" * 68)

    # ---- the story: two changes a plain diff shows as minor
    smallint_to_long = field_swap(F_STATUS, name="STATUS",
                                  ftype="esriFieldTypeInteger", length="4",
                                  domain="HydrantStatus")
    check(only(smallint_to_long, BREAKING, "Hydrants.STATUS", "Type",
               "only on an empty table"),
          "a short integer widened to a long integer is BREAKING, because "
          "Alter Field changes a type only on an empty table  <-- pinned defect")
    check(not any("Length" in c.line() for c in changes(smallint_to_long)),
          "and the length that moves with the type is not reported again")
    loosen = field_swap(F_RESULT, name="RESULT", ftype="esriFieldTypeString",
                        length="10", nullable="true")
    check(only(loosen, BREAKING, "Inspections.RESULT", "looks wider"),
          "a NOT NULL field that becomes nullable is BREAKING, not WIDENING"
          "  <-- pinned defect")
    check(only(field_swap(F_RESULT, name="RESULT", ftype="esriFieldTypeString",
                          length="10", nullable="True"), BREAKING,
               "looks wider"),
          "a NOT NULL field that becomes nullable True reads as wider too")
    tighten = field_swap(F_FACILITYID, name="FACILITYID",
                         ftype="esriFieldTypeString", length="20",
                         nullable="false")
    check(only(tighten, BREAKING, "may hold nulls"),
          "a nullable field that becomes NOT NULL is BREAKING")

    # ---- reading
    ws = load(base.encode("utf-8"))
    check(sorted(ws.datasets) == ["Hydrants", "Inspections", "Water"],
          "the fixture loads three datasets, one inside a feature dataset")
    check(sorted(ws.domains) == ["HydrantStatus", "PressureRange"],
          "and two domains")
    check(ws.datasets["Hydrants"].parent == "Water"
          and ws.datasets["Inspections"].parent == "",
          "a feature class records the feature dataset it sits in")
    check(sorted(field_map(ws.datasets["Hydrants"].elem, "x")) == sorted(
        ["OBJECTID", "Shape", "FACILITYID", "STATUS", "PRESSURE", "KIND",
         "NOTES"]),
          "the fields of an index are not read as the table's fields"
          "  <-- pinned defect")
    check(changes(base) == [], "a document compared with itself has no change")
    check(describe([])[-1] == VERDICTS[None] and EXIT_FOR[None] == 0,
          "no change is IDENTICAL and exits 0")
    other_ns = base.replace("ArcGIS/10.8", "ArcGIS/10.0").replace(
        "xmlns:esri=", "xmlns:e2=").replace("esri:", "e2:")
    check(changes(other_ns) == [],
          "another schema version and another prefix change nothing, even "
          "inside an element xwddiff has no rule for")
    qualified = base.replace("<Name>Hydrants</Name>",
                             "<Name>proddb.gisowner.Hydrants</Name>")
    check(changes(qualified) == [],
          "an owner-qualified name matches its unqualified twin")
    twin = swap(base, "<Name>Inspections</Name>",
                "<Name>other.Hydrants</Name>")
    raises(lambda: load(twin.encode("utf-8")), Ambiguous,
           "two datasets with one unqualified name refuse the document"
           "  <-- pinned defect")
    raises(lambda: load(swap(base, "<Name>Inspections</Name>",
                             "<Name> </Name>").encode("utf-8")), Ambiguous,
           "a dataset with no name refuses the document")
    dup_field = swap(base, F_INSPECTED, F_INSPECTED + F_RESULT)
    raises(lambda: changes(dup_field), Ambiguous,
           "a field named twice in one table refuses the diff instead of "
           "keeping the last one  <-- pinned defect")
    raises(lambda: load(swap(base, DOM_PRESSURE, DOM_PRESSURE + DOM_STATUS
                             ).encode("utf-8")), Ambiguous,
           "a domain named twice refuses the document")
    raises(lambda: changes(swap(base, "</CodedValues>",
                                "<CodedValue><Name>Again</Name><Code>1</Code>"
                                "</CodedValue></CodedValues>")), Ambiguous,
           "a coded value code used twice refuses the diff")
    raises(lambda: changes(swap(base, SUB_DRY, SUB_DRY + SUB_DRY)), Ambiguous,
           "a subtype code used twice refuses the diff")
    raises(lambda: changes(swap(base, "<FieldName>STATUS</FieldName><DomainName>"
                                "HydrantStatus</DomainName></SubtypeFieldInfo>",
                                "<FieldName>STATUS</FieldName></SubtypeFieldInfo>"
                                "<SubtypeFieldInfo><FieldName>STATUS"
                                "</FieldName></SubtypeFieldInfo>")),
           Ambiguous, "a subtype naming one field twice refuses the diff")
    raises(lambda: changes(swap(base, "<AliasName>Inspections</AliasName>",
                                "<AliasName>A</AliasName><AliasName>B"
                                "</AliasName>")), Ambiguous,
           "an element that may appear once appearing twice refuses the diff")
    bomb = ("<?xml version='1.0'?><!DOCTYPE lol [<!ENTITY a 'aaaaaaaaaa'>"
            "<!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;'>]>"
            "<Workspace><WorkspaceDefinition>&b;</WorkspaceDefinition>"
            "</Workspace>")
    # Without the refusal this document loads: it is well formed and has a
    # WorkspaceDefinition. So the message, not the exception, is the proof.
    raises(lambda: load(bomb.encode("utf-8")), Unreadable,
           "a DOCTYPE is refused before any entity expands  <-- pinned defect",
           ("DOCTYPE",))
    raises(lambda: load(bomb.replace("'1.0'?>", "'1.0' encoding='UTF-16'?>")
                        .encode("utf-16")), Unreadable,
           "a DOCTYPE in a UTF-16 file is refused too  <-- pinned defect",
           ("DOCTYPE",))
    utf16 = base.replace("encoding='UTF-8'", "encoding='UTF-16'")
    check(diff(ws, load(utf16.encode("utf-16"))) == [],
          "a UTF-16 document reads the same as UTF-8")
    raises(lambda: load(b"PK\x03\x04rest of a zip"), Unreadable,
           "a ZIP export is unreadable as XML")
    raises(lambda: load(b"<Workspace><unclosed></Workspace>"), Unreadable,
           "malformed XML is unreadable", ("not well-formed",))
    raises(lambda: load(b"<Other/>"), Unreadable,
           "a root that is not Workspace is unreadable", ("<Other>",))
    raises(lambda: load(b"<Workspace><Nothing/></Workspace>"), Unreadable,
           "a Workspace with no WorkspaceDefinition is unreadable",
           ("WorkspaceDefinition",))
    bare = load(b"<Workspace><WorkspaceDefinition/></Workspace>")
    check(bare.datasets == {} and bare.domains == {},
          "a document with no datasets and no domains loads empty")
    check(nothing_to_compare(bare, bare) and not nothing_to_compare(bare, ws),
          "two empty workspaces are nothing to compare, and one is not")
    raises(lambda: load(b"<Workspace><WorkspaceDefinition><A xsi:type='x'/>"
                        b"</WorkspaceDefinition></Workspace>"), Unreadable,
           "an undeclared namespace prefix is unreadable, not a traceback"
           "  <-- pinned defect", ("not well-formed",))
    raises(lambda: load(b"<?xml version='1.0' encoding='bogus'?><Workspace/>"),
           Unreadable, "an encoding Python does not know is unreadable"
           "  <-- pinned defect", ("not well-formed",))

    # ---- datasets
    no_table = swap(base, base[base.index("<DataElement xsi:type='esri:DETable'>"):
                               base.index("</DatasetDefinitions>")], "")
    check(only(no_table, BREAKING, "dataset Inspections", "removed"),
          "a dataset removed is BREAKING")
    found = changes(base, no_table)
    check(len(found) == 1 and found[0].severity == WIDENING
          and "added (DETable)" in found[0].detail,
          "a dataset added is WIDENING")
    as_fc = base.replace("<DataElement xsi:type='esri:DETable'>",
                         "<DataElement xsi:type='esri:DEFeatureClass'>")
    check(only(as_fc, BREAKING, '"DETable" -> "DEFeatureClass"'),
          "a table that becomes a feature class is BREAKING, and only once")
    moved = swap(base, "<Children xsi:type='esri:ArrayOfDataElement'>" +
                 base[base.index("<DataElement xsi:type='esri:DEFeatureClass'>"):
                      base.index("</Children>")] + "</Children>",
                 "<Children xsi:type='esri:ArrayOfDataElement'></Children>")
    moved = moved.replace("</DatasetDefinitions>", base[
        base.index("<DataElement xsi:type='esri:DEFeatureClass'>"):
        base.index("</Children>")] + "</DatasetDefinitions>")
    check(only(moved, BREAKING, "moved from feature dataset", '"Water"'),
          "a feature class moved out of its feature dataset is BREAKING")
    check(only(swap(base, "<ShapeType>esriGeometryPoint</ShapeType>",
                    "<ShapeType>esriGeometryMultipoint</ShapeType>"),
               BREAKING, "ShapeType"),
          "a changed geometry type is BREAKING")
    check(only(swap(base, "<HasZ>false</HasZ><HasSpatialIndex>",
                    "<HasZ>true</HasZ><HasSpatialIndex>"), BREAKING, "HasZ"),
          "a feature class that gains Z values is BREAKING")
    check(only(swap(base, "<AliasName>Hydrants</AliasName>",
                    "<AliasName>Fire hydrants</AliasName>"), COSMETIC,
               "AliasName"),
          "a dataset alias change is COSMETIC")
    check(only(swap(base, "<DefaultSubtypeCode>1</DefaultSubtypeCode>",
                    "<DefaultSubtypeCode>2</DefaultSubtypeCode>"), WIDENING),
          "a new default subtype code is WIDENING")
    check(only(swap(base, "<SubtypeFieldName>KIND</SubtypeFieldName>",
                    "<SubtypeFieldName>STATUS</SubtypeFieldName>"), BREAKING,
               "different field"),
          "a subtype field that changes is BREAKING")
    noise = swap(base, "<XMax>90</XMax>", "<XMax>95</XMax>")
    noise = swap(noise, "<DSID>5</DSID>", "<DSID>9</DSID>")
    noise = swap(noise, "<GridSize0>250</GridSize0>", "<GridSize0>500</GridSize0>")
    noise = swap(noise, "<HasSpatialIndex>true", "<HasSpatialIndex>false")
    noise = swap(noise, "<ConfigurationKeyword/>",
                  "<MetadataRetrieved>true</MetadataRetrieved><Metadata>"
                  "<when>today</when></Metadata><ConfigurationKeyword/>")
    check(changes(noise) == [],
          "a new extent, DSID, grid size, spatial index flag or metadata "
          "changes nothing  <-- pinned defect")
    check(changes(swap(base, "<DefaultValue xsi:type='xs:short'>1",
                       "<DefaultValue xsi:type='xs:int'>1")) == [],
          "a default of 1 typed xs:int instead of xs:short changes nothing")
    check(only(swap(base, "<HasOID>true</HasOID>",
                    "<HasOID role='x'>true</HasOID>"), BREAKING,
               "<HasOID> changed"),
          "an attribute that is not xsi:type counts in a comparison")
    check(only(swap(base, "<HasOID>true</HasOID>", "<HasOID>false</HasOID>"),
               BREAKING, "<HasOID> changed", "no rule"),
          "a dataset element with no rule that differs is BREAKING, never "
          "COSMETIC  <-- pinned defect")
    rel = base.replace("</DatasetDefinitions>",
                       "<DataElement xsi:type='esri:DERelationshipClass'>"
                       "<Name>HydrantInspections</Name><Cardinality>"
                       "esriRelCardinalityOneToMany</Cardinality></DataElement>"
                       "</DatasetDefinitions>")
    found = changes(swap(rel, "OneToMany<", "ManyToMany<"), rel)
    check(len(found) == 1 and found[0].severity == BREAKING
          and "<Cardinality> changed" in found[0].line()
          and "HydrantInspections" in found[0].line(),
          "a changed relationship class is BREAKING as unclassified")

    check(only(swap(base, "<FeatureType>esriFTSimple</FeatureType>",
                    "<FeatureType>esriFTAnnotation</FeatureType>"), BREAKING,
               "FeatureType", "no tool that changes"),
          "a changed feature type is BREAKING")
    check(only(swap(base, "<HasM>false</HasM><HasZ>false</HasZ><HasSpatialIndex>",
                    "<HasM>true</HasM><HasZ>false</HasZ><HasSpatialIndex>"),
               BREAKING, "HasM", "no tool that changes"),
          "a feature class that gains M values is BREAKING")
    check(only(swap(base, "<DatasetType>esriDTTable</DatasetType>",
                    "<DatasetType>esriDTFeatureClass</DatasetType>"), BREAKING,
               "DatasetType", "no tool converts"),
          "a dataset type that changes under one xsi:type is BREAKING")
    check(only(swap(base, "<ModelName/>", "<ModelName>HYD</ModelName>"),
               COSMETIC, "ModelName"),
          "a dataset model name change is COSMETIC")
    for label, old_xml, new_xml in (
            ("CatalogPath", "<CatalogPath>/FD=Water/FC=Hydrants</CatalogPath>",
             "<CatalogPath>/FD=Other/FC=Hydrants</CatalogPath>"),
            ("ConfigurationKeyword", "<ConfigurationKeyword/>",
             "<ConfigurationKeyword>DEFAULTS</ConfigurationKeyword>"),
            ("Versioned", "<Versioned>false</Versioned>",
             "<Versioned>true</Versioned>"),
            ("CanVersion", "<CanVersion>false</CanVersion>",
             "<CanVersion>true</CanVersion>"),
            ("HasGlobalID", "<HasGlobalID>false</HasGlobalID>",
             "<HasGlobalID>true</HasGlobalID>"),
            ("GlobalIDFieldName", "<GlobalIDFieldName/>",
             "<GlobalIDFieldName>GlobalID</GlobalIDFieldName>"),
            ("RelationshipClassNames", "<HasGlobalID>",
             "<RelationshipClassNames><Name>HydrantInspections</Name>"
             "</RelationshipClassNames><HasGlobalID>")):
        check(changes(swap(base, old_xml, new_xml)) == [],
              "a changed %s changes nothing  <-- pinned defect" % label)
    ext_one = swap(base,
                   "<PropertyArray xsi:type='esri:ArrayOfPropertySetProperty'/>",
                   "<PropertyArray><PropertySetProperty><Key>k</Key>"
                   "<Value>1</Value></PropertySetProperty></PropertyArray>")
    found = changes(ext_one.replace("<Value>1<", "<Value>2<"), ext_one)
    check(len(found) == 1 and found[0].severity == BREAKING
          and "<ExtensionProperties> changed" in found[0].detail,
          "a change two levels inside an element with no rule is BREAKING"
          "  <-- pinned defect")

    # ---- spatial reference
    def sr(old, new):
        return base.replace(SR_FT, SR_FT.replace(old, new))
    found = changes(sr("<WKID>2236</WKID>", "<WKID>6438</WKID>"))
    check(len(found) == 2 and all(c.severity == BREAKING for c in found)
          and all('WKID "2236" -> "6438"' in c.detail for c in found),
          "a new WKID is BREAKING on the feature dataset and the feature "
          "class, and the field's copy is not reported a third time")
    found = changes(sr("<XYTolerance>0.0032808333333333331</XYTolerance>",
                       "<XYTolerance>0.001</XYTolerance>"))
    check(len(found) == 2 and "XYTolerance" in found[0].detail
          and found[0].severity == BREAKING,
          "a new XY tolerance is BREAKING")
    for leaf, old_v, new_v in (("XOrigin", "-1000", "-2000"),
                               ("YOrigin", "-1000", "-3000"),
                               ("XYScale", "10000", "20000")):
        found = changes(sr("<%s>%s<" % (leaf, old_v),
                           "<%s>%s<" % (leaf, new_v)))
        check(len(found) == 2 and all(c.severity == BREAKING for c in found)
              and all(leaf in c.detail for c in found),
              "a new %s is BREAKING" % leaf)
    check(changes(sr("Synthetic_Grid_Feet", "Synthetic_Grid_Feet_v2")) == [],
          "one WKID spelled with other WKT changes nothing")
    check(changes(sr("<XYScale>10000</XYScale>",
                     "<XYScale>1e4</XYScale>")) == [],
          "10000 and 1e4 are one number, not a change")
    no_wkid = sr("<WKID>2236</WKID><LatestWKID>2236</LatestWKID>", "")
    found = changes(no_wkid.replace("Synthetic_Grid_Feet", "Other_Grid"),
                    no_wkid)
    check(len(found) == 2 and all("WKT" in c.detail for c in found),
          "without a WKID the WKT decides, and a new one is BREAKING")
    found = changes(base.replace(SR_FT, SR_FT.replace(
        "ProjectedCoordinateSystem", "GeographicCoordinateSystem")))
    check(len(found) == 2 and "type" in found[0].detail,
          "a projected system that becomes geographic is BREAKING")
    no_sr = swap(base, "<YMax>90</YMax></Extent>" + SR_FT,
                 "<YMax>90</YMax></Extent>")
    check(only(no_sr, BREAKING, "spatial reference removed"),
          "a spatial reference removed is BREAKING")
    found = changes(base, no_sr)
    check(len(found) == 1 and "spatial reference added" in found[0].detail,
          "and one added is BREAKING too")

    # ---- fields
    check(only(swap(base, F_NOTES, ""), BREAKING, "Hydrants.NOTES", "removed"),
          "a field removed is BREAKING")
    check(only(swap(base, F_NOTES, F_NOTES + _field(
        "COLOR", "esriFieldTypeString", "10")), WIDENING, "Hydrants.COLOR",
               "read null"),
          "a nullable field added is WIDENING")
    check(only(swap(base, F_NOTES, F_NOTES + _field(
        "COLOR", "esriFieldTypeString", "10", "false")), BREAKING,
               "NOT NULL", "non-empty table"),
          "a NOT NULL field added to an existing table is BREAKING"
          "  <-- pinned defect")
    check(only(swap(base, F_NOTES, F_NOTES + _field(
        "GlobalID", "esriFieldTypeGlobalID", "38", "false")), WIDENING,
               "Add Global IDs"),
          "a GlobalID field added is WIDENING, though it is NOT NULL")
    check(only(field_swap(F_NOTES, name="NOTES", ftype="esriFieldTypeString",
                          length="255", alias="Notes"), WIDENING, '"50" -> "255"'),
          "a text field lengthened is WIDENING")
    check(only(field_swap(F_NOTES, name="NOTES", ftype="esriFieldTypeString",
                          length="30", alias="Notes"), BREAKING, "shortens"),
          "a text field shortened is BREAKING")
    blob_50 = swap(base, F_NOTES, _field("NOTES", "esriFieldTypeBlob", "50"))
    blob_90 = swap(base, F_NOTES, _field("NOTES", "esriFieldTypeBlob", "90"))
    found = changes(blob_90, blob_50)
    check(len(found) == 1 and found[0].severity == WIDENING
          and "lengthen" in found[0].detail,
          "a blob field lengthened is WIDENING")
    found = changes(blob_50, blob_90)
    check(len(found) == 1 and found[0].severity == BREAKING
          and "shortens" in found[0].detail,
          "a blob field shortened is BREAKING")
    check(changes(field_swap(F_NOTES, name="NOTES", ftype="esriFieldTypeString",
                             length="50.0", alias="Notes")) == [],
          "a length of 50.0 is the length 50, not a shortening"
          "  <-- pinned defect")
    check(only(field_swap(F_NOTES, name="NOTES", ftype="esriFieldTypeString",
                          length="inf", alias="Notes"), BREAKING,
               "text or blob"),
          "a length of inf is BREAKING, not a lengthening  <-- pinned defect")
    check(changes(swap(base, F_FACILITYID, F_FACILITYID.replace(
        "<IsNullable>true", "<IsNullable>True"))) == [],
          "IsNullable True and true are one value  <-- pinned defect")
    check(only(field_swap(F_KIND, name="KIND", ftype="esriFieldTypeSmallInteger",
                          length="4"), BREAKING, "text or blob"),
          "a numeric field's length changing alone is BREAKING")
    check(only(field_swap(F_NOTES, name="NOTES", ftype="esriFieldTypeString",
                          length="fifty", alias="Notes"), BREAKING,
               "text or blob"),
          "a length that is not a number is BREAKING")
    check(only(swap(base, F_FACILITYID, F_FACILITYID.replace(
        "<Precision>0</Precision>", "<Precision>9</Precision>")), BREAKING,
               "precision or scale"),
          "a precision change is BREAKING")
    check(only(swap(base, F_FACILITYID, F_FACILITYID.replace(
        "<Scale>0</Scale>", "<Scale>2</Scale>")), BREAKING, "Scale",
               "no precision or scale parameter"),
          "a scale change is BREAKING")
    check(only(field_swap(F_NOTES, name="NOTES", ftype="esriFieldTypeString",
                          length="50", alias="Remarks"), COSMETIC, "AliasName"),
          "a field alias change is COSMETIC")
    check(only(swap(base, F_NOTES, F_NOTES.replace(
        "<ModelName>NOTES</ModelName>", "<ModelName>REMARKS</ModelName>")),
               COSMETIC, "ModelName"),
          "a model name change is COSMETIC")
    check(only(swap(base, F_NOTES, F_NOTES.replace(
        "</Field>", "<DefaultValue xsi:type='xs:string'>none</DefaultValue>"
        "</Field>")), WIDENING, "DefaultValue"),
          "a default value added is WIDENING")
    check(only(swap(base, F_INSPECTED, F_INSPECTED.replace(
        "</Field>", "<Required>true</Required></Field>")), BREAKING,
               "Required", "no parameter that changes it"),
          "a field that becomes required is BREAKING")
    check(only(swap(base, F_INSPECTED, F_INSPECTED.replace(
        "</Field>", "<Editable>false</Editable></Field>")), BREAKING,
               "<Editable> changed", "no rule"),
          "a field element with no rule that differs is BREAKING, never "
          "COSMETIC  <-- pinned defect")
    check(changes(swap(base, F_INSPECTED, F_INSPECTED.replace(
        "</Field>", "<Editable/></Field>"))) == [],
          "an empty element and an absent one are the same")
    check(changes(swap(base, F_KIND + F_NOTES, F_NOTES + F_KIND)) == [],
          "fields in a new order change nothing")
    check(only(field_swap(F_KIND, name="KIND", ftype="esriFieldTypeSmallInteger",
                          length="2", domain="HydrantStatus"), BREAKING,
               'domain (none) -> "HydrantStatus"', "valid"),
          "a domain assigned to a field is BREAKING")
    check(only(field_swap(F_STATUS, name="STATUS",
                          ftype="esriFieldTypeSmallInteger", length="2"),
               WIDENING, "any value"),
          "a domain taken off a field is WIDENING")
    check(only(field_swap(F_PRESSURE, name="PRESSURE",
                          ftype="esriFieldTypeInteger", length="4",
                          domain="OtherRange"), BREAKING, '"OtherRange"'),
          "a field moved to another domain is BREAKING")
    fat = swap(base, F_STATUS, F_STATUS.replace(
        "</DomainName></Domain>", "</DomainName><Description>x</Description>"
        "</Domain>"))
    check(changes(fat) == [],
          "a field's copy of its domain body is not compared a second time")

    # ---- indexes
    idx_unique = IDX_FACILITY.replace("<IsUnique>false", "<IsUnique>true")
    check(only(swap(base, IDX_FACILITY, idx_unique), BREAKING, "now UNIQUE"),
          "an index that becomes unique is BREAKING")
    found = changes(base, swap(base, IDX_FACILITY, idx_unique))
    check(len(found) == 1 and found[0].severity == WIDENING
          and "no longer UNIQUE" in found[0].detail,
          "an index that stops being unique is WIDENING")
    check(only(swap(base, IDX_FACILITY, ""), WIDENING, "index(FACILITYID)",
               "removed"),
          "an index removed is WIDENING")
    found = changes(base, swap(base, IDX_FACILITY, ""))
    check(len(found) == 1 and found[0].severity == WIDENING,
          "an index added is WIDENING")
    found = changes(base, swap(base, IDX_FACILITY, "").replace(IDX_OID, ""))
    check(len(found) == 2 and found[0].severity == BREAKING
          and "added as UNIQUE" in found[0].detail,
          "a unique index added is BREAKING")
    idx_pair = (IDX_FACILITY.replace("IDX_FACILITYID", "IDX_PAIR").replace(
        "</FieldArray>", "<Field xsi:type='esri:Field'><Name>STATUS</Name>"
        "</Field></FieldArray>"))
    check(only(swap(base, IDX_FACILITY, IDX_FACILITY + idx_pair), WIDENING,
               "index(FACILITYID,STATUS)", "added"),
          "an index over two fields is one index, named by both")
    check(only(swap(base, IDX_FACILITY, IDX_FACILITY + idx_pair.replace(
        "<IsUnique>false", "<IsUnique>true")), BREAKING,
               "index(FACILITYID,STATUS)", "UNIQUE"),
          "and a unique one over two fields is BREAKING")
    check(changes(swap(base, "R7_SDE_ROWID_UK", "R93_SDE_ROWID_UK")) == [],
          "an index renamed by another database's registration number "
          "changes nothing  <-- pinned defect")
    check(changes(swap(base, IDX_FACILITY, IDX_FACILITY + IDX_FACILITY.replace(
        "IDX_FACILITYID", "IDX_FACILITYID2"))) == [],
          "a second index over the same field changes nothing")

    # ---- subtypes
    check(only(swap(base, SUB_DRY, ""), BREAKING, "subtype 2", "removed"),
          "a subtype removed is BREAKING")
    found = changes(base, swap(base, SUB_DRY, ""))
    check(len(found) == 1 and found[0].severity == WIDENING
          and "subtype 2" in found[0].path,
          "a subtype added is WIDENING")
    check(only(swap(base, "Dry barrel", "Dry-barrel"), COSMETIC,
               "SubtypeName"),
          "a subtype renamed is COSMETIC")
    check(only(swap(base, SUB_DRY, SUB_DRY.replace(
        "</SubtypeFieldInfo>", "<DefaultValue>2</DefaultValue>"
        "</SubtypeFieldInfo>")), WIDENING, "subtype 2.STATUS", "DefaultValue"),
          "a subtype default value is WIDENING")
    check(only(swap(base, SUB_DRY, SUB_DRY.replace(
        "</FieldInfos>", "<SubtypeFieldInfo><FieldName>PRESSURE</FieldName>"
        "<DomainName>PressureRange</DomainName></SubtypeFieldInfo>"
        "</FieldInfos>")), BREAKING, "subtype 2.PRESSURE",
               '"PressureRange"'),
          "a domain given to a field in one subtype is BREAKING")
    check(only(swap(base, SUB_DRY, SUB_DRY.replace(
        "<DomainName>HydrantStatus</DomainName>", "")), WIDENING,
               "subtype 2.STATUS", "any value"),
          "a domain taken off a field in one subtype is WIDENING")

    # ---- domains
    check(only(swap(base, DOM_PRESSURE, ""), BREAKING,
               "domain PressureRange", "removed"),
          "a domain removed is BREAKING")
    found = changes(base, swap(base, DOM_PRESSURE, ""))
    check(len(found) == 1 and found[0].severity == WIDENING,
          "a domain added is WIDENING")
    check(only(swap(base, DOM_PRESSURE, DOM_PRESSURE.replace(
        "esri:RangeDomain", "esri:CodedValueDomain")), BREAKING,
               '"RangeDomain" -> "CodedValueDomain"'),
          "a range domain that becomes coded is BREAKING, and only once")
    check(only(swap(base, DOM_PRESSURE, DOM_PRESSURE.replace(
        "<FieldType>esriFieldTypeInteger", "<FieldType>esriFieldTypeDouble")),
               BREAKING, "FieldType"),
          "a domain's field type change is BREAKING")
    check(only(swap(base, "<CodedValue><Name>Out of service</Name><Code "
                    "xsi:type='xs:short'>2</Code></CodedValue>", ""),
               BREAKING, "code 2", "Out of service"),
          "a coded value removed is BREAKING")
    check(only(swap(base, "</CodedValues>", "<CodedValue><Name>Abandoned"
                    "</Name><Code>3</Code></CodedValue></CodedValues>"),
               WIDENING, "code 3", "Abandoned"),
          "a coded value added is WIDENING")
    check(only(swap(base, "<Name>Out of service</Name>",
                    "<Name>Out of order</Name>"), COSMETIC, "code 2",
               "description"),
          "a coded value description change is COSMETIC")
    check(changes(swap(base, "<Name>In service</Name><Code xsi:type='xs:short'>"
                      "1</Code>", "<Code xsi:type='xs:short'> 1 </Code>"
                      "<Name>In service</Name>")) == [],
          "a code's position and padding change nothing")

    def bound(old, new):
        return swap(base, DOM_PRESSURE, DOM_PRESSURE.replace(old, new))
    check(only(bound(">0</MinValue>", ">20</MinValue>"), BREAKING,
               "MinValue", "outside the new range"),
          "a range minimum raised is BREAKING")
    check(only(bound(">0</MinValue>", ">-5</MinValue>"), WIDENING, "MinValue"),
          "a range minimum lowered is WIDENING")
    check(only(bound(">200</MaxValue>", ">150</MaxValue>"), BREAKING,
               "MaxValue"),
          "a range maximum lowered is BREAKING")
    check(only(bound(">200</MaxValue>", ">250</MaxValue>"), WIDENING,
               "MaxValue"),
          "a range maximum raised is WIDENING")
    check(changes(bound(">200</MaxValue>", ">200.0</MaxValue>")) == [],
          "200 and 200.0 are one bound")
    check(only(bound(">200</MaxValue>", ">high</MaxValue>"), BREAKING,
               "no rule"),
          "a bound that is not a number is BREAKING")
    check(only(bound("<SplitPolicy>esriSPTDefaultValue",
                     "<SplitPolicy>esriSPTDuplicate"), WIDENING, "SplitPolicy"),
          "a split policy change is WIDENING")
    check(only(bound("<MergePolicy>esriMPTDefaultValue",
                     "<MergePolicy>esriMPTSumValues"), WIDENING, "MergePolicy"),
          "a merge policy change is WIDENING")
    for word in ("nan", "inf"):
        check(only(bound(">200</MaxValue>", ">%s</MaxValue>" % word), BREAKING,
                   "no rule"),
              "a range bound of %s is BREAKING, not WIDENING  <-- pinned "
              "defect" % word)
    inf_default = swap(base, F_NOTES, F_NOTES.replace(
        "</Field>", "<DefaultValue>inf</DefaultValue></Field>"))
    found = changes(inf_default.replace(">inf<", ">Infinity<"), inf_default)
    check(len(found) == 1 and found[0].severity == WIDENING,
          "a default of inf and one of Infinity are two defaults, not a "
          "number  <-- pinned defect")
    check(only(bound("<Owner/>", "<Owner>gisowner</Owner>"), COSMETIC, "Owner"),
          "a domain owner change is COSMETIC")
    check(only(bound("psi</Description>", "PSI</Description>"), COSMETIC,
               "Description"),
          "a domain description change is COSMETIC")

    # ---- verdict and report
    mixed = swap(base, F_NOTES, "")
    mixed = swap(mixed, "Dry barrel", "Dry-barrel")
    mixed = swap(mixed, DOM_PRESSURE, DOM_PRESSURE.replace(">200</Max",
                                                           ">250</Max"))
    found = changes(mixed)
    lines = describe(found)
    check(worst(found) == BREAKING and EXIT_FOR[worst(found)] == 1,
          "one BREAKING change among others makes the verdict BREAKING, exit 1")
    check(lines[0].startswith("BREAKING") and lines[1].startswith("WIDENING")
          and lines[2].startswith("COSMETIC"),
          "the report lists BREAKING first, then WIDENING, then COSMETIC")
    check(lines[-2] == "changes: 1 BREAKING, 1 WIDENING, 1 COSMETIC",
          "and counts each class")
    check(worst([Change(WIDENING, "k", "p", "d"),
                 Change(COSMETIC, "k", "p", "d")]) == WIDENING
          and EXIT_FOR[WIDENING] == 4,
          "WIDENING as the worst change exits 4")
    check(worst([Change(COSMETIC, "k", "p", "d")]) == COSMETIC
          and EXIT_FOR[COSMETIC] == 0,
          "COSMETIC as the worst change exits 0")

    # ---- the CLI, against real files
    def tmpfile(name, body):
        path = os.path.join(tmp, name)
        with open(path, "wb") as handle:
            handle.write(body.encode("utf-8") if isinstance(body, str)
                         else body)
        return path

    def run_cli(argv):
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code, out.getvalue(), err.getvalue()

    before = tmpfile("before.xml", base)
    after_break = tmpfile("break.xml", smallint_to_long)
    after_wide = tmpfile("wide.xml", swap(base, F_NOTES, F_NOTES + _field(
        "COLOR", "esriFieldTypeString", "10")))
    after_cosm = tmpfile("cosm.xml", swap(base, "Dry barrel", "Dry-barrel"))
    after_twin = tmpfile("twin.xml", twin)
    out_csv = os.path.join(tmp, "changes.csv")

    code, out, err = run_cli([before, before])
    check(code == 0 and "VERDICT: IDENTICAL" in out,
          "the CLI exits 0 on two identical documents")
    code, out, err = run_cli([before, after_break])
    check(code == 1 and "VERDICT: BREAKING" in out
          and "Hydrants.STATUS" in out,
          "the CLI exits 1 on a BREAKING change and names it")
    check(run_cli([before, after_wide])[0] == 4,
          "the CLI exits 4 when WIDENING is the worst change")
    check(run_cli([before, after_cosm])[0] == 0,
          "the CLI exits 0 when only COSMETIC changes")
    code, out, err = run_cli([before, after_break])
    check("Check only" not in out and "wrote" not in out,
          "a run with no --out says nothing about writing  <-- pinned defect")
    empty = tmpfile("empty.xml", "<Workspace><WorkspaceDefinition/></Workspace>")
    code, out, err = run_cli([empty, empty])
    check(code == 2 and "nothing was compared" in err
          and "IDENTICAL" not in out,
          "two documents with no dataset and no domain exit 2, not IDENTICAL"
          "  <-- pinned defect")
    check(run_cli([before, empty])[0] == 1,
          "a full document against an empty one is every dataset removed")
    code, out, err = run_cli([before, tmpfile(
        "prefix.xml", "<Workspace><WorkspaceDefinition><A xsi:type='x'/>"
        "</WorkspaceDefinition></Workspace>")])
    check(code == 2 and "not well-formed" in err,
          "the CLI exits 2 on an undeclared prefix, not 1  <-- pinned defect")
    deep = swap(base, "<ModelName/>",
                "<ModelName/>" + "<D>" * 1500 + "</D>" * 1500)
    code, out, err = run_cli([tmpfile("deep.xml", deep), tmpfile(
        "deep2.xml", deep.replace("<D>", "<D k='1'>", 1))])
    check(code == 2 and "nested too deeply" in err,
          "a document nested past the recursion limit exits 2, not 1"
          "  <-- pinned defect")
    code, out, err = run_cli([before, after_break, "--out", before, "--apply"])
    with open(before, "rb") as handle:
        kept = handle.read()
    check(code == 64 and "input documents" in err
          and kept == base.encode("utf-8"),
          "--out naming an input document is refused and the input is kept"
          "  <-- pinned defect")
    code, out, err = run_cli([before, after_twin, "--out", out_csv,
                              "--apply"])
    check(code == 3 and "REFUSED" in out and "VERDICT: REFUSED" in out,
          "an ambiguous document exits 3, not the 1 of a breaking change")
    check(not os.path.exists(out_csv),
          "and a refused run writes nothing, even with --apply"
          "  <-- pinned defect")
    code, out, err = run_cli([before, after_break, "--out", out_csv])
    check(code == 1 and "Check only" in out and not os.path.exists(out_csv),
          "--out without --apply writes nothing at all  <-- pinned defect")
    code, out, err = run_cli([before, tmpfile("mixed.xml", mixed),
                              "--out", out_csv, "--apply"])
    with open(out_csv, newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    check(code == 1 and len(written) == 3
          and [r["severity"] for r in written] == list(SEVERITIES),
          "--out with --apply writes one row per change, worst first")
    every_kind = swap(base, F_NOTES, "")
    every_kind = swap(every_kind, IDX_FACILITY, "")
    every_kind = swap(every_kind, "Dry barrel", "Dry-barrel")
    every_kind = swap(every_kind, DOM_PRESSURE, "")
    every_kind = swap(every_kind, "<AliasName>Hydrants</AliasName>",
                      "<AliasName>Fire hydrants</AliasName>")
    check(sorted(c.kind for c in changes(every_kind)) == [
        "dataset", "domain", "field", "index", "subtype"],
          "each kind of change is named: dataset, field, index, subtype, domain")
    formula = swap(base, "<Name>Inspections</Name>", "<Name>=cmd</Name>")
    formula_csv = os.path.join(tmp, "formula.csv")
    code, out, err = run_cli([tmpfile("f.xml", formula), before, "--out",
                              formula_csv, "--apply"])
    with open(formula_csv, newline="", encoding="utf-8") as handle:
        cells = list(csv.DictReader(handle))
    check(code == 1 and cells[0]["path"] == "'=cmd",
          "a name that starts with = is written with a leading quote, so a "
          "spreadsheet does not run it  <-- pinned defect")
    check(cell("Hydrants") == "Hydrants" and cell("") == "",
          "and an ordinary cell is written as it is")
    line_break = swap(base, "<AliasName>Hydrants</AliasName>",
                      "<AliasName>x\nVERDICT: IDENTICAL</AliasName>")
    report = describe(changes(line_break))
    check(len(report) == 3 and "\\n" in report[0]
          and report[-1].startswith("VERDICT: COSMETIC"),
          "a line break in an alias prints as a backslash n and forges no "
          "report line  <-- pinned defect")
    wide_text = swap(base, "<AliasName>Hydrants</AliasName>",
                     u"<AliasName>Hydrants \u2265 2in \u6c34</AliasName>")
    wide_path = tmpfile("wide_text.xml", wide_text)
    raw = io.BytesIO()
    ascii_out = io.TextIOWrapper(raw, encoding="ascii")
    old_out = sys.stdout
    sys.stdout = ascii_out
    try:
        code = main([before, wide_path])
        ascii_out.flush()
    finally:
        sys.stdout = old_out
    check(code == 0 and b"\\u2265" in raw.getvalue(),
          "a name the console cannot encode prints as an escape, and a "
          "COSMETIC run still exits 0  <-- pinned defect")
    code, out, err = run_cli([before, after_break, "--out", tmp, "--apply"])
    check(code == 2 and "could not write" in err,
          "an --out that cannot be written exits 2, not the gate's 1")
    check(run_cli([os.path.join(tmp, "absent.xml"), before])[0] == 2,
          "a document that is not there exits 2")
    check(run_cli([tmp, before])[0] == 2,
          "a directory where a document should be exits 2")
    code, out, err = run_cli([before, tmpfile("bomb.xml", bomb)])
    check(code == 2 and "DOCTYPE" in err,
          "the CLI exits 2 on a DOCTYPE and says why")
    code, out, err = run_cli([before, tmpfile("export.zip", b"PK\x03\x04z")])
    check(code == 2 and "Extract the .xml" in err,
          "a ZIP export exits 2 and is named as one, not as bad XML")
    code, out, err = run_cli([before])
    check(code == 64 and "two XML workspace documents" in err,
          "one document is a usage error")
    code, out, err = run_cli([before, before, "--apply"])
    check(code == 64 and "--apply needs --out" in err,
          "--apply without --out is a usage error")
    code, out, err = run_cli([before, before, "--out", out_csv, "--ap"])
    check(code == 64 and "unrecognized" in err,
          "a unique prefix --ap of --apply is refused by the parser"
          "  <-- pinned defect")
    check(run_cli(["--self"])[0] == 64,
          "a prefix of --self-test is refused too")
    argv_before = sys.argv
    try:
        sys.argv = ["xwddiff.py", before, after_cosm]
        check(run_cli(None)[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, ValueError, "probe: no exception must fail")
        raises(lambda: [][0], ValueError, "probe: the wrong exception must fail")
        raises(lambda: int("x"), ValueError, "probe: a missing word must fail",
               ("absent word",))
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    sys.stdout = io.StringIO()
    try:
        probe_code = summary(3, ["probe"])
        probe_text = sys.stdout.getvalue()
    finally:
        sys.stdout = quiet
    check(probe_code == 1 and "3 assertions, 1 failed" in probe_text
          and "FAILED: probe" in probe_text,
          "a failed run prints the count and every failed label, and exits 1")
    check(len(probe) == 4,
          "check() and raises() really do record a failure  <-- pinned defect")
    raises(lambda: swap("aa", "a", "b"), AssertionError,
           "the fixture refuses a fragment that is not unique")

    print("-" * 68)
    return summary(passed[0] + len(failed), failed)


def summary(total, failed):
    """The self-test footer, and its exit code."""
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # 64, not argparse's 2: here 2 means a document could not be read.
        self.print_usage(sys.stderr)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        sys.exit(EXIT_USAGE)


def _parse(argv):
    ap = _Parser(
        prog="xwddiff.py",
        description="Classify the diff between two XML workspace documents "
                    "as BREAKING, WIDENING or COSMETIC.",
        epilog="Exit 0 identical or cosmetic, 1 breaking, 2 unreadable, "
               "3 refused, 4 widening, 64 usage. Nothing is written without "
               "--apply.",
        allow_abbrev=False,
    )
    ap.add_argument("old", nargs="?", metavar="OLD",
                    help="the document the target geodatabase matches today")
    ap.add_argument("new", nargs="?", metavar="NEW",
                    help="the document it is meant to match next")
    ap.add_argument("--out", help="path for a CSV of every change")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def read_document(path):
    with open(path, "rb") as handle:
        return load(handle.read())


def cell(value):
    """A CSV cell that a spreadsheet will not run as a formula."""
    if value[:1] in tuple("=+-@" + chr(9) + chr(13)):
        return "'" + value
    return value


def write_changes(path, changes):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(OUT_COLUMNS)
        for severity in SEVERITIES:
            for c in changes:
                if c.severity == severity:
                    writer.writerow([c.severity, c.kind, cell(c.path),
                                     cell(c.detail)])


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(b))


def safe_streams():
    """Print what the console cannot encode as escapes, not as a traceback.

    A traceback exits 1, which a job reads as BREAKING.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="backslashreplace")


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    safe_streams()

    if args.self_test:
        return self_test()
    if not args.old or not args.new:
        print("error: two XML workspace documents are required, OLD then "
              "NEW. Use --self-test to verify the tool without them.",
              file=sys.stderr)
        return EXIT_USAGE
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return EXIT_USAGE
    if args.out and (same_path(args.out, args.old)
                     or same_path(args.out, args.new)):
        print("error: --out names one of the two input documents. Name a new "
              "file.", file=sys.stderr)
        return EXIT_USAGE

    print("xwddiff: %s -> %s" % (args.old, args.new))
    try:
        old, new = read_document(args.old), read_document(args.new)
        if nothing_to_compare(old, new):
            raise Unreadable("neither document holds a dataset or a domain "
                             "under <WorkspaceDefinition>, so nothing was "
                             "compared")
        found = diff(old, new)
    except RecursionError:
        print("error: a document is nested too deeply to compare.",
              file=sys.stderr)
        return EXIT_UNREADABLE
    except Ambiguous as exc:
        print("REFUSED   %s. A name that stands for two things cannot be "
              "diffed, so nothing was compared." % exc)
        print("VERDICT: REFUSED")
        return EXIT_REFUSED
    except (Unreadable, IOError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_UNREADABLE

    for line in describe(found):
        print(line)

    if args.out:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
        else:
            # A traceback would exit 1, which a job reads as BREAKING.
            try:
                write_changes(args.out, found)
            except (IOError, OSError) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return EXIT_UNREADABLE
            print("\nwrote %s" % args.out)

    return EXIT_FOR[worst(found)]


# The false side is the module being imported, which the self-test run never
# does, so it is excluded from branch coverage on purpose.
if __name__ == "__main__":  # pragma: no branch
    sys.exit(main())
