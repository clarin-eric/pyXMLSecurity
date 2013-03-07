from UserDict import DictMixin

__author__ = 'leifj'

import os
import rsa_x509_pem
import lxml.etree as etree
import logging
import base64
import hashlib
import copy
import int_to_bytes as itb
from lxml.builder import ElementMaker
from exceptions import XMLSigException

NS = {'ds': 'http://www.w3.org/2000/09/xmldsig#'}
DS = ElementMaker(namespace=NS['ds'],nsmap=NS)

# Enable this to get various parts written to files in /tmp. Not for production!
_DEBUG_WRITE_TO_FILES = False

# ASN.1 BER SHA1 algorithm designator prefixes (RFC3447)
ASN1_BER_ALG_DESIGNATOR_PREFIX = { \
    # disabled 'md2': '\x30\x20\x30\x0c\x06\x08\x2a\x86\x48\x86\xf7\x0d\x02\x02\x05\x00\x04\x10',
    # disabled 'md5': '\x30\x20\x30\x0c\x06\x08\x2a\x86\x48\x86\xf7\x0d\x02\x05\x05\x00\x04\x10',
    'sha1':   '\x30\x21\x30\x09\x06\x05\x2b\x0e\x03\x02\x1a\x05\x00\x04\x14',
    'sha256': '\x30\x31\x30\x0d\x06\x09\x60\x86\x48\x01\x65\x03\x04\x02\x01\x05\x00\x04\x20',
    'sha384': '\x30\x41\x30\x0d\x06\x09\x60\x86\x48\x01\x65\x03\x04\x02\x02\x05\x00\x04\x30',
    'sha512': '\x30\x51\x30\x0d\x06\x09\x60\x86\x48\x01\x65\x03\x04\x02\x03\x05\x00\x04\x40',
    }

import re, htmlentitydefs

TRANSFORM_ENVELOPED_SIGNATURE = 'http://www.w3.org/2000/09/xmldsig#enveloped-signature'
TRANSFORM_C14N_EXCLUSIVE_WITH_COMMENTS = 'http://www.w3.org/2001/10/xml-exc-c14n#WithComments'
TRANSFORM_C14N_EXCLUSIVE = 'http://www.w3.org/2001/10/xml-exc-c14n'
TRANSFORM_C14N_INCLUSIVE = 'http://www.w3.org/TR/2001/REC-xml-c14n-20010315'

ALGORITHM_DIGEST_SHA1 = "http://www.w3.org/2000/09/xmldsig#sha1"
ALGORITHM_SIGNATURE_RSA_SHA1 = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"

# This code was inspired by https://github.com/andrewdyates/xmldsig
# and includes https://github.com/andrewdyates/rsa_x509_pem with
# permission from the author.

class CertDict(DictMixin):
    """
    Extract all X509Certificate XML elements and create a dict-like object
    to access the certificates.
    """
    def __init__(self,t):
        """
        :param t: XML as lxml.etree
        """
        self.certs = {}
        for cd in t.findall(".//{%s}X509Certificate" % NS['ds']):
            cert_pem = cd.text
            cert_der = base64.b64decode(cert_pem)
            m = hashlib.sha1()
            m.update(cert_der)
            fingerprint = m.hexdigest().lower()
            fingerprint = ":".join([fingerprint[x:x+2] for x in xrange(0,len(fingerprint),2)])
            self.certs[fingerprint] = cert_pem

    def __getitem__(self, item):
        return self.certs[item]

    def keys(self):
        return self.certs.keys()

    def __setitem__(self, key, value):
        self.certs[key] = value

    def __delitem__(self, key):
        del self.certs[key]

def _find_matching_cert(t,fp):
    for hash,pem in CertDict(t).iteritems():
        if fp == hash:
            return pem
    return None

def _cert(sig,keyspec):
    """
    Look for X.509 certificate for keyspec.

    Search order :
      1. in file indicated by keyspec
      2. in Signature element, with fingerprint indicated by keyspec
      3. in keyspec itself (keyspec is the cert in this case)

    :param sig: Signature element as lxml.Element
    :param keyspec: X.509 cert filename, string with fingerprint or X.509 cert as string
    :returns: X.509 cert as string
    """
    data = None
    if os.path.isfile(keyspec):
        with open(keyspec) as c:
            data = c.read()
    elif ':' in keyspec:
        cd = _find_matching_cert(sig,keyspec)
        if cd is not None:
            data = "-----BEGIN CERTIFICATE-----\n%s\n-----END CERTIFICATE-----" % cd
    else:
        data = keyspec

    if data is None:
        raise XMLSigException("Unable to find anything useful to verify with")

    return data

def _root(t):
    if hasattr(t,'getroot') and hasattr(t.getroot,'__call__'):
        return t.getroot()
    else:
        return t

def number_of_bits(num):
    """
    Return the number of bits required to represent num.

    In python >= 2.7, there is num.bit_length().

    NOTE: This function appears unused, so it might go away.
    """
    assert num>=0
    # this is much faster than you would think, AND it is easy to read ;)
    return len(bin(num)) - 2

b64d = lambda s: s.decode('base64')

def b64e(s):
    if type(s) in (int, long):
        s = itb.int_to_bytes(s)
    return s.encode('base64').replace('\n', '')

def _signed_value(data, key_size, do_pad, hash_alg): # TODO Do proper asn1 CMS
    """Return unencrypted rsa-sha1 signature value `padded_digest` from `data`.

    The resulting signed value will be in the form:
    (01 | FF* | 00 | prefix | digest) [RSA-SHA1]
    where "digest" is of the generated c14n xml for <SignedInfo>.

    Args:
      data: str of bytes to sign
      key_size: int of key length in bits; => len(`data`) + 3
    Returns:
      str: rsa-sha1 signature value of `data`
    """

    prefix = ASN1_BER_ALG_DESIGNATOR_PREFIX.get(hash_alg)
    if not prefix:
        raise XMLSigException("Unknown hash algorithm %s" % hash_alg)
    asn_digest = prefix + data
    if do_pad:
        # Pad to "one octet shorter than the RSA modulus" [RSA-SHA1]
        # WARNING: key size is in bits, not bytes!
        padded_size = key_size/8 - 1
        pad_size = padded_size - len(asn_digest) - 2
        pad = '\x01' + '\xFF' * pad_size + '\x00'
        return pad + asn_digest
    else:
        return asn_digest

def _digest(data, hash_alg):
    """
    Calculate a hash digest of algorithm hash_alg and return the result base64 encoded.

    :param hash_alg: String with algorithm, such as 'sha1'
    :param data: The data to digest
    :returns: Base64 string
    """
    h = getattr(hashlib,hash_alg)()
    h.update(data)
    digest = b64e(h.digest())
    return digest

def _get_by_id(t,id_v):
    for id_a in _id_attributes:
        logging.debug("Looking for #%s using id attribute '%s'" % (id_v,id_a))
        elts = t.xpath("//*[@%s='%s']" % (id_a,id_v))
        if elts is not None and len(elts) > 0:
            return elts[0]
    return None

def _alg(elt):
    """
    Return the hashlib name of an Algorithm. Hopefully.
    :returns: None or string
    """
    uri = elt.get('Algorithm',None)
    if uri is None:
        return None
    else:
        return uri.rstrip('#')

def _remove_child_comments(t):
    root = _root(t)
    for c in root.iter():
        if c.tag is etree.Comment or c.tag is etree.PI:
            _delete_elt(c)
    return t

def _process_references(t,sig=None):
    """
    :returns: hash algorithm as string
    """
    if sig is None:
        sig = t.find(".//{%s}Signature" % NS['ds'])
    hash_alg = None
    for ref in sig.findall(".//{%s}Reference" % NS['ds']):
        object = None
        uri = ref.get('URI',None)
        if uri is None or uri == '#' or uri == '':
            ct = _remove_child_comments(copy.deepcopy(t))
            object = _root(ct)
        elif uri.startswith('#'):
            ct = copy.deepcopy(t)
            object = _root(_get_by_id(ct,uri[1:]))
        else:
            raise XMLSigException("Unknown reference %s" % uri)

        if object is None:
            raise XMLSigException("Unable to dereference Reference URI='%s'" % uri)

        for tr in ref.findall(".//{%s}Transform" % NS['ds']):
            logging.debug("transform: %s" % _alg(tr))
            object = _transform(_alg(tr),object,tr)

        if _DEBUG_WRITE_TO_FILES:
            with open("/tmp/foo-obj.xml","w") as fd:
                fd.write(object)

        dm = ref.find(".//{%s}DigestMethod" % NS['ds'])
        if dm is None:
            raise XMLSigException("Unable to find DigestMethod")
        this_hash_alg = (_alg(dm).split("#"))[1]
        logging.debug("using hash algorithm %s" % this_hash_alg)
        hash_alg = hash_alg or this_hash_alg
        if this_hash_alg != hash_alg:
            raise XMLSigException("Unable to handle more than one hash algorithm (%s != %s)" \
                                      % (this_hash_alg, hash_alg))
        digest = _digest(object,this_hash_alg)
        logging.debug("digest for %s: %s" % (uri,digest))
        dv = ref.find(".//{%s}DigestValue" % NS['ds'])
        logging.debug(etree.tostring(dv))
        dv.text = digest
    return hash_alg

##
# Removes HTML or XML character references and entities from a text string.
#
# @param text The HTML (or XML) source text.
# @return The plain text, as a Unicode string, if necessary.

def _unescape(text):
    def fixup(m):
        text = m.group(0)
        if text[:2] == "&#":
            # character reference
            try:
                if text[:3] == "&#x":
                    return unichr(int(text[3:-1], 16))
                else:
                    return unichr(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                if not text in ('&amp;','&lt;','&gt;'):
                    text = unichr(htmlentitydefs.name2codepoint[text[1:-1]])
            except KeyError:
                pass
        return text # leave as is
    return re.sub("&#?\w+;", fixup, text)

def _delete_elt(elt):
    assert elt.getparent() is not None,XMLSigException("Cannot delete root")
    if elt.tail is not None:
        logging.debug("tail: '%s'" % elt.tail)
        p = elt.getprevious()
        if p is not None:
            logging.debug("adding tail to previous")
            if p.tail is None:
                p.tail = ''
            p.tail += elt.tail
        else:
            logging.debug("adding tail to parent")
            up = elt.getparent()
            assert up is not None,XMLSigException("Signature has no parent")
            if up.text is None:
                up.text = ''
            up.text += elt.tail
    elt.getparent().remove(elt)

def _enveloped_signature(t):
    sig = t.find('.//{http://www.w3.org/2000/09/xmldsig#}Signature')
    _delete_elt(sig)
    if _DEBUG_WRITE_TO_FILES:
        with open("/tmp/foo-env.xml","w") as fd:
            fd.write(etree.tostring(t))
    return t

def _c14n(t,exclusive,with_comments,inclusive_prefix_list=None):
    """
    Perform XML canonicalization (c14n) on an lxml.etree.

    NOTE: The c14n done here is missing whitespace removal. The whitespace has to
    be removed at parse time. One way to do that is to use xmlsec.parse_xml().

    :param t: XML as lxml.etree
    :param exclusive: boolean
    :param with_comments: boolean, keep comments or not
    :param inclusive_prefix_list: List of namespaces to include (?)
    :returns: XML as string (utf8)
    """
    cxml = etree.tostring(t,method="c14n",exclusive=exclusive,with_comments=with_comments,inclusive_ns_prefixes=inclusive_prefix_list)
    u = _unescape(cxml.decode("utf8",'replace')).encode("utf8").strip()
    assert u[0] == '<',XMLSigException("C14N buffer doesn't start with '<'")
    assert u[-1] == '>',XMLSigException("C14N buffer doesn't end with '>'")
    return u

def _transform(uri,t,tr=None):
    if uri == TRANSFORM_ENVELOPED_SIGNATURE:
        return _enveloped_signature(t)

    if uri == TRANSFORM_C14N_EXCLUSIVE_WITH_COMMENTS:
        nslist = None
        if tr is not None:
            elt = tr.find(".//{%s}InclusiveNamespaces" % 'http://www.w3.org/2001/10/xml-exc-c14n#')
            if elt is not None:
                nslist = elt.get('PrefixList','').split()
        return _c14n(t,exclusive=True,with_comments=True,inclusive_prefix_list=nslist)

    if uri == TRANSFORM_C14N_EXCLUSIVE:
        nslist = None
        if tr is not None:
            elt = tr.find(".//{%s}InclusiveNamespaces" % 'http://www.w3.org/2001/10/xml-exc-c14n#')
            if elt is not None:
                nslist = elt.get('PrefixList','').split()
        return _c14n(t,exclusive=True,with_comments=False,inclusive_prefix_list=nslist)

    if uri == TRANSFORM_C14N_INCLUSIVE:
        return _c14n(t,exclusive=False,with_comments=False)

    raise XMLSigException("unknown or unimplemented transform %s" % uri)

_id_attributes =['ID','id']
def setID(ids):
    _id_attributes = ids

def pem2b64(pem):
    return '\n'.join(pem.strip().split('\n')[1:-1])

def b642pem(data):
    x = data
    r = "-----BEGIN CERTIFICATE-----\n"
    while len(x) > 64:
        r += x[0:64]
        r += "\n"
        x = x[64:]
    r += x
    r += "\n"
    r += "-----END CERTIFICATE-----"
    return r

def pem2cert(pem):
    return rsa_x509_pem.parse(pem)

def b642cert(data):
    return rsa_x509_pem.parse(b642pem(data))

def verify(t,keyspec):
    """
    Verify the signature(s) in an XML document.

    Throws an XMLSigException on any non-matching signatures.

    :param t: XML as lxml.etree
    :param keyspec: X.509 cert filename, string with fingerprint or X.509 cert as string
    :returns: True if signature(s) validated, False if there were no signatures
    """
    if _DEBUG_WRITE_TO_FILES:
        with open("/tmp/foo-sig.xml","w") as fd:
            fd.write(etree.tostring(_root(t)))
    validated = False
    for sig in t.findall(".//{%s}Signature" % NS['ds']):
        sv = sig.findtext(".//{%s}SignatureValue" % NS['ds'])
        assert sv is not None,XMLSigException("No SignatureValue")

        data = _cert(sig,keyspec)
        cert = rsa_x509_pem.parse(data)
        key = rsa_x509_pem.get_key(cert)
        key_f_public = rsa_x509_pem.f_public(key)

        expected = key_f_public(b64d(sv))

        hash_alg = _process_references(t,sig)
        if _DEBUG_WRITE_TO_FILES:
            with open("/tmp/foo-ref.xml","w") as fd:
                fd.write(etree.tostring(_root(t)))
        si = sig.find(".//{%s}SignedInfo" % NS['ds'])
        b_digest = _create_signature_digest(si, hash_alg)

        sz = int(key.size())+1
        logging.debug("key size: %d" % sz)
        actual = _signed_value(b_digest, sz, True, hash_alg)

        assert expected == actual,XMLSigException("Signature validation failed")
        validated = True

    return validated

## TODO - support transforms with arguments
def _signed_info_transforms(transforms):
    ts = [DS.Transform(Algorithm=t) for t in transforms]
    return DS.Transforms(*ts)

# standard enveloped rsa-sha1 signature
def _enveloped_signature_template(c14n_method,digest_alg,transforms,reference_uri):
    return DS.Signature(
        DS.SignedInfo(
            DS.CanonicalizationMethod(Algorithm=c14n_method),
            DS.SignatureMethod(Algorithm=ALGORITHM_SIGNATURE_RSA_SHA1),
            DS.Reference(
                _signed_info_transforms(transforms),
                DS.DigestMethod(Algorithm=digest_alg),
                DS.DigestValue(),
                URI=reference_uri
            )
        )
    )

def add_enveloped_signature(t,c14n_method=TRANSFORM_C14N_INCLUSIVE,digest_alg=ALGORITHM_DIGEST_SHA1,transforms=None,reference_uri=""):
    if transforms is None:
        transforms = (TRANSFORM_ENVELOPED_SIGNATURE,TRANSFORM_C14N_EXCLUSIVE_WITH_COMMENTS)
    _root(t).insert(0,_enveloped_signature_template(c14n_method,digest_alg,transforms,reference_uri))

def sign(t,key_spec,cert_spec=None,reference_uri=""):
    """
    Sign an XML document.

    :param t: XML as lxml.etree
    :param key_spec: instance with attr __call__, PEM filename or 'pkcs11://' URL
    :param cert_spec: None, X.509 cert as string or X.509 cert filename
        None is only valid if key_spec is a pkcs11:// URL
    :param reference_uri: Envelope signature reference URI
    :returns: XML as lxml.etree (for convenience, 't' is modified in-place)
    """
    cert_data = None
    key_f_private = None
    do_padding = False # only in the case of our fallback keytype do we need to do pkcs1 padding here

    if hasattr(key_spec,'__call__'):
        key_f_private = key_spec
    elif os.path.isfile(key_spec):
        key_data = open(key_spec).read()
        priv_key = rsa_x509_pem.parse(key_data)
        key_f_private = rsa_x509_pem.f_private(rsa_x509_pem.get_key(priv_key))
        do_padding = True # need to do p1 padding in this case
    elif key_spec.startswith("pkcs11://"):
        import pk11
        key_f_private,cert_data = pk11.signer(key_spec)
        logging.debug("Using pkcs11 singing key: %s" % key_f_private)
    else:
        raise XMLSigException("Unable to load private key from '%s'" % key_spec)

    assert key_f_private is not None,XMLSigException("Can I haz key?")

    if cert_data is None and cert_spec is not None:
        if 'BEGIN CERTIFICATE' in cert_spec:
            cert_data = cert_spec
        elif os.path.exists(cert_spec):
            cert_data = open(cert_spec).read()

    assert cert_data is not None,XMLSigException("Unable to find certificate to go with key %s" % key_spec)

    cert = rsa_x509_pem.parse(cert_data)
    pub_key = rsa_x509_pem.get_key(cert)
    key_f_public = rsa_x509_pem.f_public(pub_key)
    sz = int(pub_key.size())+1

    logging.debug("Using %s bit key" % sz)

    if t.find(".//{%s}Signature" % NS['ds']) is None:
        add_enveloped_signature(t,reference_uri=reference_uri)

    for sig in t.findall(".//{%s}Signature" % NS['ds']):
        hash_alg = _process_references(t,sig)
        if _DEBUG_WRITE_TO_FILES:
            with open("/tmp/sig-ref.xml","w") as fd:
                fd.write(etree.tostring(_root(t)))

        si = sig.find(".//{%s}SignedInfo" % NS['ds'])
        b_digest = _create_signature_digest(si, hash_alg)

        tbs = _signed_value(b_digest,sz,do_padding,hash_alg)
        signed = key_f_private(tbs)
        sv = b64e(signed)
        logging.debug("SignedValue: %s" % sv)
        si.addnext(DS.SignatureValue(sv))
        # Insert cert_data as b64-encoded X.509 certificate into XML document
        sv_elt = si.getnext()
        sv_elt.addnext(DS.KeyInfo(DS.X509Data(DS.X509Certificate(pem2b64(cert_data)))))

    return t

def _create_signature_digest(si, hash_alg):
    """
    :param hash_alg: string such as 'sha1'
    """
    cm = si.find(".//{%s}CanonicalizationMethod" % NS['ds'])
    cm_alg = _alg(cm)
    assert cm is not None and cm_alg is not None,XMLSigException("No CanonicalizationMethod")
    sic = _transform(cm_alg,si)
    logging.debug("SignedInfo C14N: %s" % sic)
    digest = _digest(sic, hash_alg)
    logging.debug("SignedInfo digest: %s" % digest)
    return b64d(digest)

def parse_xml(data, remove_whitespace=True):
    """
    Parse XML data into an lxml.etree and remove whitespace in the process.

    :param data: XML as string
    :param remove_whitespace: boolean
    :returns: XML as lxml.etree
    """
    parser = etree.XMLParser(remove_blank_text=remove_whitespace)
    return etree.XML(data, parser)
