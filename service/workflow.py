from octopus.core import app
from octopus.modules.epmc import client as epmc
from octopus.modules.doaj import client as doaj
from service import models, sheets, licences
import os

class WorkflowException(Exception):
    pass

def csv_upload(flask_file_handle, filename, contact_email):
    # make a record of the upload
    s = models.SpreadsheetJob()
    s.filename = filename
    s.contact_email = contact_email
    s.status_code = "submitted"
    s.makeid()

    # find out where to put the file
    upload = app.config.get("UPLOAD_DIR")
    if upload is None or upload == "":
        raise WorkflowException("UPLOAD_DIR is not set")

    # save the file and the record of the upload
    flask_file_handle.save(os.path.join(upload, s.id + ".csv"))
    s.save()

def normalise_pmcid(pmcid):
    return pmcid

def normalise_pmid(pmid):
    return pmid

def normalise_doi(doi):
    return doi

def parse_csv(job):
    # find out where to get the file
    upload = app.config.get("UPLOAD_DIR")
    if upload is None or upload == "":
        raise WorkflowException("UPLOAD_DIR is not set")

    path = os.path.join(upload, job.id + ".csv")
    sheet = sheets.MasterSheet(path)

    i = 0
    for obj in sheet.objects():
        i += 1
        r = models.Record()
        r.upload_id = job.id
        r.upload_pos = i
        r.set_source_data(**obj)

        # also copy the various identifiers over into the locations where they can be normalised
        # and used for lookup

        if obj.get("pmcid") is not None and obj.get("pmcid") != "":
            r.pmcid = normalise_pmcid(obj.get("pmcid"))
            note = "normalised PMCID %(source)s to %(target)s" % {"source" : obj.get("pmcid"), "target" : r.pmcid }
            r.add_provenance("importer", note)

        if obj.get("pmid") is not None and obj.get("pmid") != "":
            r.pmid = normalise_pmid(obj.get("pmid"))
            note = "normalised PMCD %(source)s to %(target)s" % {"source" : obj.get("pmid"), "target" : r.pmid }
            r.add_provenance("importer", note)

        if obj.get("doi") is not None and obj.get("doi") != "":
            r.doi = normalise_doi(obj.get("doi"))
            note = "normalised DOI %(source)s to %(target)s" % {"source" : obj.get("doi"), "target" : r.doi }
            r.add_provenance("importer", note)

        if obj.get("article_title") is not None and obj.get("article_title") != "":
            r.title = obj.get("article_title")

        r.save()


class WorkflowMessage(object):
    def __init__(self, job=None, record=None, oag_register=None):
        self.job = job
        self.record = record
        self.oag_register = oag_register

def process_jobs():
    """
    Process all of the jobs in the index which are of status "submitted"
    :return:nothing
    """
    jobs = models.SpreadsheetJob.list_by_status("submitted")
    for job in jobs:
        process_job(job)

def process_job(job):
    """
    Process the spreadsheet job in its entirity

    :param job: models.SpreadsheetJob object
    :return:    nothing
    """

    # start by switching the status of the job so it is active
    job.status_code = "processing"
    job.save()

    # list all of the records, and work through them one by one doing all the processing
    records = models.Record.list_by_upload(job.id)
    oag_register = []
    for record in records:
        msg = WorkflowMessage(job, record, oag_register)
        process_record(msg)

    # the oag_register will now contain all the records that need to go on to OAG
    process_oag(oag_register, job)

    # beyond this point all the processing is handled asynchronously, so this function
    # is now complete

def process_record(msg):
    """
    Process an individual record (as represented by the workflow message)

    :param msg:  WorkflowMessage object containing the record to process
    :return:
    """
    # get the epmc metadata for this record
    epmc_md, confidence = get_epmc_md(msg)
    if epmc_md is None:
        # if no metadata, then we have to give up
        note = "unable to locate any metadata record in EPMC for the combination of identifiers/title; giving up"
        msg.record.add_provenance("processor", note)
        return

    # set the confidence that we have accurately identified this record
    msg.record.confidence = confidence

    # populate the missing identifiers
    populate_identifiers(msg, epmc_md)

    # add the key bits of metadata we're interested in
    extract_metadata(msg, epmc_md)

    # now we've extracted all we can from the EPMC metadata, let's save before moving on to the
    # next external request
    msg.record.save()

    # obtain the fulltext, and if found, extract metadata and licence information from it
    fulltext = get_epmc_fulltext(msg)
    if fulltext is not None:
        extract_fulltext_info(msg, fulltext)
        extract_fulltext_licence(msg, fulltext)

        # since we have extracted data, and are about to do another external request, save again
        msg.record.save()

    # lookup the issn in the DOAJ, and record whether the journal is OA or hybrid
    oajournal = doaj_lookup(msg)
    msg.record.journal_type = "oa" if oajournal else "hybrid"
    if oajournal:
        msg.record.add_provenance("processor", "Journal with ISSN $(issn)s was found in DOAJ; assuming OA" % {"issn" : msg.record.issn})
    else:
        msg.record.add_provenance("processor", "Journal with ISSN $(issn)s was not found in DOAJ; assuming Hybrid" % {"issn" : msg.record.issn})

    # if necessary, register an identifier to be looked up in OAG
    register_with_oag(msg)

    # finally, save the record in its current state, which is as far as we can go with it
    msg.record.save()

    # after this, all additional work will be picked up by the OAG processing chain, asynchronously

def process_oag(oag_register, job):
    # delayed imports because this function is a prototype and we'll wholesale replace it in the full service
    import json, requests
    postdata = json.dumps(oag_register)
    resp = requests.post("http://howopenisit.org/lookup", postdata)
    licences = json.loads(resp.text)

    # in the full service there will be a single callback that will take the OAGR state object and
    # extract the successes and errors - this is just a stand in, and oag_record_callback is where we will
    # initially put all the callback functionality.
    oag_rerun = []
    for r in licences.get("results", []):
        oag_record_callback(r, oag_rerun)
    for e in licences.get("errors", []):
        oag_record_callback(e, oag_rerun)

    # FIXME: in the full service, this is next bit is no good.  We will instead need to query the index
    # and determine if all the records are complete.  This might involve marking the records as complete
    # at some stage.

    # at this stage we now have a list of new identifiers which need to be re-run (or an empty list)
    if len(oag_rerun) == 0:
        job.status_code = "complete"
        job.save()
    else:
        # FIXME: note that this could result in exceeding the maximum stack depth if we aren't careful.
        # the full service won't be allowed to behave like this
        process_oag(oag_rerun, job)


def get_epmc_md(msg):
    # look using the pmcid first
    if msg.record.pmcid is not None:
        mds = epmc.EuropePMC.get_by_pmcid(msg.record.pmcid)
        if len(mds) == 1:
            return mds[0], 1.0

    # if we find 0 or > 1 via the pmcid, try again with the pmid
    if msg.record.pmid is not None:
        mds = epmc.EuropePMC.get_by_pmid(msg.record.pmid)
        if len(mds) == 1:
            return mds[0], 1.0

    # if we find 0 or > 1 via the pmid, try again with the doi
    if msg.record.doi is not None:
        mds = epmc.EuropePMC.get_by_doi(msg.record.doi)
        if len(mds) == 1:
            return mds[0], 1.0

    if msg.record.title is not None:
        mds = epmc.EuropePMC.title_exact(msg.record.title)
        if len(mds) == 1:
            return mds[0], 0.9
        mds = epmc.EuropePMC.title_approximate(msg.record.title)
        if len(mds) == 1:
            return mds[0], 0.7

    return None, None


def register_with_oag(msg):
    # if we have a pmcid, then we register it for lookup if the licence is missing OR if the AAM information
    # has not been retrieved yet
    if msg.record.pmcid is not None:
        if msg.record.aam_from_xml and msg.record.licence_type is not None:
            return
        else:
            msg.oag_register.append({"id" : msg.record.pmcid, "type" : "pmcid"})
            msg.record.in_oag = True
            msg.record.oag_pmcid = "sent"
            msg.record.save()
            return

    # in all other cases, if the licence has already been detected, we don't need to do any more
    if msg.record.licence_type is not None:
        return

    # next priority is to send a doi if there is one
    if msg.record.doi is not None:
        msg.oag_register.append({"id" : msg.record.doi, "type" : "doi"})
        msg.record.in_oag = True
        msg.record.oag_doi = "sent"
        msg.record.save()
        return

    # lowest priority is to send the pmid if there is one
    if msg.record.pmid is not None:
        msg.oag_register.append({"id" : msg.record.pmid, "type" : "pmid"})
        msg.record.in_oag = True
        msg.record.oag_pmid = "sent"
        msg.record.save()
        return

    # if we get to here, then something is wrong with this record, and we can't send it to OAG
    return


def get_epmc_fulltext(msg):
    """
    Get the fulltext record if it exists
    :param msg: WorkflowMessage object
    :return: EPMCFulltext object or None if not found
    """
    if msg.record.pmcid is None:
        return None

    try:
        ft = epmc.EuropePMC.fulltext(msg.record.pmcid)
        return ft
    except epmc.EPMCFullTextException:
        return None

def doaj_lookup(msg):
    """
    Lookup the issn in the record in the DOAJ.  If we find it, this means the journal
    is pure OA

    :param msg: WorkflowMessage object
    :return:    True if the journal is OA, False if hybrid
    """
    client = doaj.DOAJSearchClient()
    journals = client.journal_by_issn(msg.record.issn)
    return len(journals) > 0

def populate_identifiers(msg, epmc_md):
    """
    Any identifiers which are present in the EPMC metadata but not in the source
    record should be copied over

    :param msg:     WorkflowMessage object
    :param epmc_md:     EPMC metadata object
    :return:
    """
    if msg.record.pmcid is None and epmc_md.pmcid is not None:
        msg.record.pmcid = epmc_md.pmcid

    if msg.record.pmid is None and epmc_md.pmid is not None:
        msg.record.pmid = epmc_md.pmid

    if msg.record.doi is None and epmc_md.doi is not None:
        msg.record.doi = epmc_md.doi

def extract_metadata(msg, epmc_md):
    """
    Extract the inEPMC and isOA properties of the metadata

    :param msg: WorkflowMessage object
    :param epmc_md: EPMC Metadata object
    :return:
    """
    if epmc_md.in_epmc is not None:
        msg.record.in_epmc = epmc_md.in_epmc

    if epmc_md.is_oa is not None:
        msg.record.is_oa = epmc_md.is_oa

    if epmc_md.issn is not None:
        msg.record.issn = epmc_md.issn

def extract_fulltext_info(msg, fulltext):
    # record that the fulltext exists in the first place
    msg.record.has_ft_xml = True
    msg.record.add_provenance("processor", "Found fulltext XML in EPMC")

    # record whether the fulltext tells us this is an author manuscript
    msg.record.aam = fulltext.is_aam
    msg.record.aam_from_xml = True

def extract_fulltext_licence(msg, fulltext):
    type, url, para = fulltext.get_licence_details()

    if type in licences.urls.values():
        msg.record.licence_type = type
        msg.record.add_provenance("processor", "Fulltext XML specifies licence type as %(license)s" % {"license" : type})
        msg.record.licence_source = "epmc_xml"

    if url in licences.urls:
        msg.record.licence_type = licences.urls[url]
        msg.record.add_provenance("processor", "Fulltext XML specifies licence url as %(url)s which gives us licence type %(license)s" % {"url" : url, "license" : licences.urls[url]})
        msg.record.licence_source = "epmc_xml"

    for ss, t in licences.substrings.iteritems():
        if ss in para:
            msg.record.licence_type = t
            msg.record.add_provenance("processor", "Fulltext XML licence description contains the licence text $(text)s which gives us licence type %(license)s" % {"text" : ss, "license" : t})
            msg.record.licence_source = "epmc_xml"
            break

def oag_record_callback(result, oag_rerun):
    def handle_error(record, idtype, oag_rerun):
        # first record an error status against the id type
        if idtype == "pmcid":
            record.oag_pmcid = "error"
        elif idtype == "pmid":
            record.oag_pmid = "error"
        elif idtype == "doi":
            record.oag_doi = "error"

        # save the record then pass it on to see if it needs to be re-submitted
        record.save()
        add_to_rerun(record, idtype, oag_rerun)


    def handle_fto(record, idtype, oag_rerun):
        # first record an error status against the id type
        if idtype == "pmcid":
            record.oag_pmcid = "fto"
        elif idtype == "pmid":
            record.oag_pmid = "fto"
        elif idtype == "doi":
            record.oag_doi = "fto"

        # save the record then pass it on to see if it needs to be re-submitted
        record.save()
        add_to_rerun(record, idtype, oag_rerun)

    def handle_success(result, record, idtype):
        # first record an error status against the id type
        if idtype == "pmcid":
            record.oag_pmcid = "success"
            record.licence_source = "epmc"
        elif idtype == "pmid":
            record.oag_pmid = "success"
            record.licence_source = "publisher"
        elif idtype == "doi":
            record.oag_doi = "success"
            record.licence_source = "publisher"

        record.licence_type = result.get("license", [{}])[0].get("type")
        record.save()
        return

    def add_to_rerun(record, idtype, oag_rerun):
        if idtype == "pmcid":
            if record.doi is not None:
                oag_rerun.append({"id" : record.doi, "type" : "doi"})
                return
            if record.pmid is not None:
                oag_rerun.append({"id" : record.pmid, "type" : "pmid"})
                return
            return
        elif idtype == "doi":
            if record.pmid is not None:
                oag_rerun.append({"id" : record.pmid, "type" : "pmid"})
                return
            return
        elif idtype == "pmid":
            return


    def process_licence(result, record, idtype, oag_rerun):
        # if the record already has a licence, we don't do anything
        if record.licence_type is not None:
            record.save()
            return

        # get the OAG provenance description and put it into the record
        prov = result.get("license", [{}])[0].get("provenance", {}).get("description")
        record.add_provenance("oag", prov)

        if result.get("license", [{}])[0].get("type") == "failed-to-obtain-license":
            handle_fto(record, idtype, oag_rerun)
        else:
            handle_success(result, record, idtype)

    iserror = False

    # start by getting the identifier of the object that has been processed
    ident = result.get("identifier")
    if isinstance(ident, list):
        id = ident[0].get("id")
        type = ident[0].get("type")
    else:
        iserror = True
        id = ident.get("id")
        type = ident.get("type")

    # FIXME: in the full service we need to find a way to ensure that we always can find the type or the record
    # based only on the identifier, and also that we are looking at the identifier in the context of the correct sheet.
    # This probably means either:
    # a) extending OAGR to allow us to store arbitrary metadata along with the state
    # b) storing an explicit relationship between the OAGR state and the spreadsheet job in another table
    if id is None or type is None:
        print "insufficient data to relate OAG response to"
        return

    # now locate the related record
    record = models.Record.get_by_identifier(type, id)
    assert isinstance(record, models.Record)    # For pycharm type inspection

    # set its in_oag flag and re-save it
    record.in_oag(False)
    record.save()

    # FIXME: by this point we must know the type
    if type == "pmcid":
        if iserror:
            handle_error(record, "pmcid", oag_rerun)
            return
        else:
            if not record.aam_from_xml:
                # FIXME: this is where we'll get the AAM data from the OAG record once it has the update
                pass
            process_licence(result, record, type, oag_rerun)
    elif type == "doi":
        if iserror:
            handle_error(record, "doi", oag_rerun)
            return
        else:
            process_licence(result, record, type, oag_rerun)
    elif type == "pmid":
        if iserror:
            handle_error(record, "pmid", oag_rerun)
        else:
            process_licence(result, record, type, oag_rerun)





