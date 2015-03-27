#!/usr/bin/env python2
# vim:fileencoding=utf-8
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'

import textwrap, os
from io import BytesIO

from lxml import etree
from lxml.builder import ElementMaker

from calibre import guess_type
from calibre.constants import numeric_version, __appname__
from calibre.ebooks.docx.names import namespaces, STYLES, WEB_SETTINGS, IMAGES, FONTS
from calibre.ebooks.metadata import authors_to_string
from calibre.ebooks.metadata.opf2 import OPF as ReadOPF
from calibre.ebooks.oeb.base import OPF, OPF2_NS
from calibre.utils.date import utcnow
from calibre.utils.localization import canonicalize_lang, lang_as_iso639_1
from calibre.utils.zipfile import ZipFile

def xml2str(root, pretty_print=False, with_tail=False):
    if hasattr(etree, 'cleanup_namespaces'):
        etree.cleanup_namespaces(root)
    ans = etree.tostring(root, encoding='utf-8', xml_declaration=True,
                          pretty_print=pretty_print, with_tail=with_tail)
    return ans

def update_doc_props(root, mi):
    def setm(name, text=None, ns='dc'):
        ans = root.makeelement('{%s}%s' % (namespaces[ns], name))
        for child in tuple(root):
            if child.tag == ans.tag:
                root.remove(child)
        ans.text = text
        root.append(ans)
        return ans
    setm('title', mi.title)
    setm('creator', authors_to_string(mi.authors))
    if mi.tags:
        setm('keywords', ', '.join(mi.tags), ns='cp')
    if mi.comments:
        setm('description', mi.comments)
    if mi.languages:
        l = canonicalize_lang(mi.languages[0])
        setm('language', lang_as_iso639_1(l) or l)


class DocumentRelationships(object):

    def __init__(self):
        self.rmap = {}
        for typ, target in {
                STYLES: 'styles.xml',
                WEB_SETTINGS: 'webSettings.xml',
                FONTS: 'fontTable.xml',
        }.iteritems():
            self.add_relationship(target, typ)

    def get_relationship_id(self, target, rtype, target_mode=None):
        return self.rmap.get((target, rtype, target_mode))

    def add_relationship(self, target, rtype, target_mode=None):
        ans = self.get_relationship_id(target, rtype, target_mode)
        if ans is None:
            ans = 'rId%d' % (len(self.rmap) + 1)
            self.rmap[(target, rtype, target_mode)] = ans
        return ans

    def add_image(self, target):
        return self.add_relationship(target, IMAGES)

    def serialize(self):
        E = ElementMaker(namespace=namespaces['pr'], nsmap={None:namespaces['pr']})
        relationships = E.Relationships()
        for (target, rtype, target_mode), rid in self.rmap.iteritems():
            r = E.Relationship(Id=rid, Type=rtype, Target=target)
            if target_mode is not None:
                r.set('TargetMode', target_mode)
            relationships.append(r)
        return xml2str(relationships)

class DOCX(object):

    def __init__(self, opts, log):
        self.opts, self.log = opts, log
        self.document_relationships = DocumentRelationships()
        self.font_table = etree.Element('{%s}fonts' % namespaces['w'], nsmap={k:namespaces[k] for k in 'wr'})
        E = ElementMaker(namespace=namespaces['pr'], nsmap={None:namespaces['pr']})
        self.embedded_fonts = E.Relationships()
        self.fonts = {}

    # Boilerplate {{{
    @property
    def contenttypes(self):
        E = ElementMaker(namespace=namespaces['ct'], nsmap={None:namespaces['ct']})
        types = E.Types()
        for partname, mt in {
            "/word/footnotes.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
            "/word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            "/word/numbering.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
            "/word/styles.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
            "/word/endnotes.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
            "/word/settings.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
            "/word/theme/theme1.xml": "application/vnd.openxmlformats-officedocument.theme+xml",
            "/word/fontTable.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml",
            "/word/webSettings.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.webSettings+xml",
            "/docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
            "/docProps/app.xml": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        }.iteritems():
            types.append(E.Override(PartName=partname, ContentType=mt))
        added = {'png', 'gif', 'jpeg', 'jpg', 'svg', 'xml'}
        for ext in added:
            types.append(E.Default(Extension=ext, ContentType=guess_type('a.'+ext)[0]))
        for ext, mt in {
            "rels": "application/vnd.openxmlformats-package.relationships+xml",
            "odttf": "application/vnd.openxmlformats-officedocument.obfuscatedFont",
        }.iteritems():
            added.add(ext)
            types.append(E.Default(Extension=ext, ContentType=mt))
        for fname in self.images:
            ext = fname.rpartition(os.extsep)[-1]
            if ext not in added:
                added.add(ext)
                mt = guess_type('a.' + ext)[0]
                if mt:
                    types.append(E.Default(Extension=ext, ContentType=mt))
        return xml2str(types)

    @property
    def appproperties(self):
        E = ElementMaker(namespace=namespaces['ep'], nsmap={None:namespaces['ep']})
        props = E.Properties(
            E.Application(__appname__),
            E.AppVersion('%02d.%04d' % numeric_version[:2]),
            E.DocSecurity('0'),
            E.HyperlinksChanged('false'),
            E.LinksUpToDate('true'),
            E.ScaleCrop('false'),
            E.SharedDoc('false'),
        )
        if self.mi.publisher:
            props.append(E.Company(self.mi.publisher))
        return xml2str(props)

    @property
    def containerrels(self):
        return textwrap.dedent(b'''\
        <?xml version='1.0' encoding='utf-8'?>
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
            <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
            <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
        </Relationships>''')

    @property
    def websettings(self):
        E = ElementMaker(namespace=namespaces['w'], nsmap={'w':namespaces['w']})
        ws = E.webSettings(
            E.optimizeForBrowser, E.allowPNG, E.doNotSaveAsSingleFile)
        return xml2str(ws)

    # }}}

    def convert_metadata(self, oeb):
        E = ElementMaker(namespace=namespaces['cp'], nsmap={x:namespaces[x] for x in 'cp dc dcterms xsi'.split()})
        cp = E.coreProperties(E.revision("1"), E.lastModifiedBy('calibre'))
        ts = utcnow().isoformat(str('T')).rpartition('.')[0] + 'Z'
        for x in 'created modified'.split():
            x = cp.makeelement('{%s}%s' % (namespaces['dcterms'], x), **{'{%s}type' % namespaces['xsi']:'dcterms:W3CDTF'})
            x.text = ts
            cp.append(x)
        package = etree.Element(OPF('package'), attrib={'version': '2.0'}, nsmap={None: OPF2_NS})
        oeb.metadata.to_opf2(package)
        self.mi = ReadOPF(BytesIO(xml2str(package)), populate_spine=False, try_to_guess_cover=False).to_book_metadata()
        update_doc_props(cp, self.mi)
        return xml2str(cp)

    def write(self, path_or_stream, oeb):
        with ZipFile(path_or_stream, 'w') as zf:
            zf.writestr('[Content_Types].xml', self.contenttypes)
            zf.writestr('_rels/.rels', self.containerrels)
            zf.writestr('docProps/core.xml', self.convert_metadata(oeb))
            zf.writestr('docProps/app.xml', self.appproperties)
            zf.writestr('word/webSettings.xml', self.websettings)
            zf.writestr('word/document.xml', xml2str(self.document))
            zf.writestr('word/styles.xml', xml2str(self.styles))
            zf.writestr('word/fontTable.xml', xml2str(self.font_table))
            zf.writestr('word/_rels/document.xml.rels', self.document_relationships.serialize())
            zf.writestr('word/_rels/fontTable.xml.rels', xml2str(self.embedded_fonts))
            for fname, data_getter in self.images.iteritems():
                zf.writestr(fname, data_getter())
            for fname, data in self.fonts.iteritems():
                zf.writestr(fname, data)

if __name__ == '__main__':
    d = DOCX(None, None)
    print (d.websettings)
