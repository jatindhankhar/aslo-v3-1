import os
import shutil
import configparser
from flask import current_app as app
from aslo.celery_app import logger
from subprocess import call
from polib import pofile
from .exceptions import BuildProcessError
import requests
import zipfile
import json
from glob import glob
import uuid

def get_translations(activity_location):
    po_files_location = os.path.join(activity_location,'po/')
    translations = {}
    
    def get_language_code(filepath):
        basename = os.path.basename(filepath)
        return os.path.splitext(basename)[0]
    
    matched_files = glob(os.path.join(po_files_location,"*.po"))
    if len(matched_files) == 0:
        raise BuildProcessError("No po files found at location . %s",po_files_location)

    po_files = list(map(pofile,matched_files))
    language_codes = list(map(get_language_code,matched_files))

    # Intialize the dictionary
    for language_code in language_codes:
        translations[language_code] = {}

    for po_file,language_code in zip(po_files,language_codes):
        for entry in po_file.translated_entries():
            #print(entry)
            translations[language_code][entry.msgid] = entry.msgstr    

    return translations

def get_repo_location(name):
    return os.path.join(app.config['BUILD_CLONE_REPO'], name)


def get_bundle_path(bundle_name):
    return os.path.join(app.config['BUILD_BUNDLE_DIR'], bundle_name)

    
def get_parser(activity_file, read_string=False):
    parser = configparser.ConfigParser()
    if read_string:
        try:
            parser.read_string(activity_file)
        except Exception as e:
            raise BuildProcessError('Error parsing metadata file. Error : %s',e)
        else:
            return parser
    else:
        if len(parser.read(activity_file)) == 0:
            raise BuildProcessError('Error parsing metadata file')
        else:
            return parser


def validate_metadata_attributes(parser, attributes):
    MANDATORY_ATTRIBUTES = ['name', 'bundle_id', 'summary', 'license',
                            'categories', 'icon', 'activity_version', 'repository', 'activity_version']
    return all(parser.has_option('Activity', attribute) for attribute in attributes)


def check_and_download_assets(assets):

    def check_asset(asset):
        if asset_name_check(asset['name']):
            return asset_manifest_check(asset['browser_download_url'], asset['name'])
        return False

    def download_asset(download_url, name):
        response = requests.get(download_url, stream=True)
        # Save with every block of 1024 bytes
        logger.info("Downloading File .. " + name)
        with open(os.path.join(app.config['TEMP_BUNDLE_DIR'],name), "wb") as handle:
            for block in response.iter_content(chunk_size=1024):
                handle.write(block)
        return

    def check_info_file(name):
        logger.info("Checking For Activity.info")
        xo_file = zipfile.ZipFile(os.path.join(app.config['TEMP_BUNDLE_DIR'],name))
        return any("activity.info" in filename for filename in xo_file.namelist())

    def asset_name_check(asset_name):
        print("Checking for presence of .xo in name of " + asset_name)
        return ".xo" in asset_name

    def verify_bundle(bundle_name):
        bundle_path = get_bundle_path(bundle_name)
        return os.path.exists(bundle_path) and os.path.isfile(bundle_path)

    def asset_manifest_check(download_url, bundle_name):
        download_asset(download_url, bundle_name)
        if check_info_file(bundle_name):
            # Check if that bundle already exists then we don't continue
            # Return false if that particular bundle already exists
            if verify_bundle(bundle_name):
                os.remove(os.path.join(app.config['TEMP_BUNDLE_DIR'],bundle_name))
                raise BuildProcessError('File %s already exits' % bundle_name)
            else:
                shutil.move(os.path.join(app.config['TEMP_BUNDLE_DIR'],
                            bundle_name), app.config['BUILD_BUNDLE_DIR'])
                return bundle_name
        return False

    for asset in assets:
        bundle_name = check_asset(asset)
        if bundle_name:
            return bundle_name
    raise BuildProcessError('No valid bundles were found in this asset release')


def clone_repo(url, name, tag):
    target_dir = app.config['BUILD_CLONE_REPO']
    if not os.path.isdir(target_dir):
        raise BuildProcessError('Directory %s does not exist' % target_dir)

    if os.path.isdir(get_repo_location(name)):
        logger.info('Removing existing cloned repo for %s', name)
        shutil.rmtree(get_repo_location(name))

    cmd = ['git', '-c', 'advice.detachedHead=false', '-C', target_dir,
           'clone', '-b', tag, '--depth', '1', url]

    logger.info('Cloning repo %s', url)
    if call(cmd) != 0:
        raise BuildProcessError('[%s] command has failed' % ' '.join(cmd))


def get_activity_metadata(name):
    def metadata_file_exists():
        repo_dir = get_repo_location(name)
        activity_file = os.path.join(repo_dir, "activity/activity.info")
        if not os.path.isfile(activity_file):
            raise BuildProcessError(
                'Activity file %s does not exist', activity_file
            )

        return activity_file

    def parse_metadata_file():
        parser = get_parser(activity_file)
        try:
            attributes = dict(parser.items('Activity'))
        except configparser.NoSectionError as e:
            raise BuildProcessError(
                'Error parsing metadata file. Exception message: %s', e
            )

        return attributes

    activity_file = metadata_file_exists()
    return parse_metadata_file()


def invoke_build(name):
    def store_bundle():
        dist_dir = os.path.join(get_repo_location(name), 'dist')
        if os.path.isdir(dist_dir) and len(os.listdir(dist_dir)) == 1:
            bundle_name = os.path.join(dist_dir, os.listdir(dist_dir)[0])
        else:
            raise BuildProcessError('Bundle file was not generated correctly')

        try:
            shutil.copy2(bundle_name, app.config['BUILD_BUNDLE_DIR'])
            stored_bundle = os.path.join(
                app.config['BUILD_BUNDLE_DIR'],
                os.path.basename(bundle_name)
            )
            os.chmod(stored_bundle, 0o644)
        except IOError as e:
            raise BuildProcessError(
                'Bundle copying has failed: %s', e
            )

        logger.info('Bundle succesfully stored at %s', stored_bundle)

    def clean():
        shutil.rmtree(get_repo_location(name))

    volume = get_repo_location(name) + ':/activity'
    docker_image = app.config['BUILD_DOCKER_IMAGE']
    docker_cmd = ['docker', 'run', '--rm', '-v', volume, docker_image]
    logger.info('Running docker command: "%s"', ' '.join(docker_cmd))
    if call(docker_cmd) != 0:
        raise BuildProcessError('Docker building process has failed')

    store_bundle()
    clean()

def get_xo_translations(bundle_name):
    logger.info("Opening translations")
    def clean_up(extact_dir):
        shutil.rmtree(extact_dir)
    try:
        logger.info(get_bundle_path(bundle_name))
        xo_archive = zipfile.ZipFile(get_bundle_path(bundle_name))
        # Create a random UUID to store the extracted material
        random_uuid = uuid.uuid4().hex
        # Create the folder with name as random UUID
        extract_dir = os.path.join(app.config['TEMP_BUNDLE_DIR'],random_uuid)
        os.mkdir(extract_dir)
        logger.info(extract_dir)
        # Find root_prefix for the activities usually it's Name.Activity
        archive_root_prefix = os.path.commonpath(xo_archive.namelist())
        xo_archive.extractall(path=extract_dir)
        translations = get_translations(os.path.join(extract_dir,archive_root_prefix))       
        # Clean up
        clean_up(extract_dir)
        return translations
    except Exception as e:
        # If exception is cause due to FileExistError, probably that two uuids were same somehow then clean up
        if e.__class__.__name__ is "FileExistsError":
            clean_up(extract_dir)
        raise BuildProcessError("Unable to open archive : %s. Error : %s ",bundle_name,e.__class__)        


def invoke_asset_build(bundle_name):
    def remove_bundle(bundle_name):
        logger.info("Removing Bundle : %s",bundle_name)
        os.remove(get_bundle_path(bundle_name))
 
    def parse_metadata_file():
        parser = get_parser(activity_file,read_string=True)
        try:
            attributes = dict(parser.items('Activity'))
        except configparser.NoSectionError as e:
            raise BuildProcessError(
                'Error parsing metadata file. Exception message: %s', e
            )

        return attributes
  
    def check_bundle(bundle_name):
        xo_file = zipfile.ZipFile(get_bundle_path(bundle_name))
        # Find the acitivity_file and return it
        for filename in xo_file.namelist():
            if 'activity.info' in filename:
                return xo_file.read(filename)
        logger.info(
            'Bundle Check has failed. %s is not a valid bundle file ', bundle_name)
        raise BuildProcessError(
            'Bundle Check has failed. %s is not a valid bundle file ', bundle_name)

    try:
        activity_file = check_bundle(bundle_name)
        activity_file = activity_file.decode()
        return parse_metadata_file()
    except Exception as e:
        remove_bundle(bundle_name)        
        raise BuildProcessError(
            'Error decoding MeteData File. Exception Message: %s', e)
        
