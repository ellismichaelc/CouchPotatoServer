from couchpotato import get_session
from couchpotato.core.event import addEvent, fireEventAsync, fireEvent
from couchpotato.core.helpers.encoding import toUnicode, simplifyString
from couchpotato.core.logger import CPLog
from couchpotato.core.settings.model import EpisodeLibrary, SeasonLibrary, LibraryTitle, File
from couchpotato.core.media._base.library import LibraryBase
from couchpotato.core.helpers.variable import tryInt
from string import ascii_letters
import time
import traceback

log = CPLog(__name__)


class EpisodeLibraryPlugin(LibraryBase):

    default_dict = {'titles': {}, 'files':{}}

    def __init__(self):
        addEvent('library.title', self.title)
        addEvent('library.identifier', self.identifier)
        addEvent('library.add.episode', self.add)
        addEvent('library.update.episode', self.update)
        addEvent('library.update.episode_release_date', self.updateReleaseDate)

    def title(self, library, first=True, condense=False, include_identifier=True):
        if library is list or library.get('type') != 'episode':
            return

        # Get the titles of the season
        if not library.get('related_libraries', {}).get('season', []):
            log.warning('Invalid library, unable to determine title.')
            return

        titles = fireEvent(
            'library.title',
            library['related_libraries']['season'][0],
            first=False,
            include_identifier=include_identifier,
            condense=condense,

            single=True
        )

        identifier = fireEvent('library.identifier', library, single = True)

        # Add episode identifier to titles
        if include_identifier and identifier.get('episode'):
            titles = [title + ('E%02d' % identifier['episode']) for title in titles]


        if first:
            return titles[0] if titles else None

        return titles


    def identifier(self, library):
        if library.get('type') != 'episode':
            return

        identifier = {
            'season': None,
            'episode': None
        }

        scene_map = library['info'].get('map_episode', {}).get('scene')

        if scene_map:
            # Use scene mappings if they are available
            identifier['season'] = scene_map.get('season')
            identifier['episode'] = scene_map.get('episode')
        else:
            # Fallback to normal season/episode numbers
            identifier['season'] = library.get('season_number')
            identifier['episode'] = library.get('episode_number')


        # Cast identifiers to integers
        # TODO this will need changing to support identifiers with trailing 'a', 'b' characters
        identifier['season'] = tryInt(identifier['season'], None)
        identifier['episode'] = tryInt(identifier['episode'], None)

        return identifier

    def add(self, attrs = {}, update_after = True):
        type = attrs.get('type', 'episode')
        primary_provider = attrs.get('primary_provider', 'thetvdb')

        db = get_session()
        parent_identifier = attrs.get('parent_identifier',  None)

        parent = None
        if parent_identifier:
            parent = db.query(SeasonLibrary).filter_by(primary_provider = primary_provider,  identifier = attrs.get('parent_identifier')).first()

        l = db.query(EpisodeLibrary).filter_by(type = type, identifier = attrs.get('identifier')).first()
        if not l:
            status = fireEvent('status.get', 'needs_update', single = True)
            l = EpisodeLibrary(
                type = type,
                primary_provider = primary_provider,
                year = attrs.get('year'),
                identifier = attrs.get('identifier'),
                plot = toUnicode(attrs.get('plot')),
                tagline = toUnicode(attrs.get('tagline')),
                status_id = status.get('id'),
                info = {},
                parent = parent,
                season_number = tryInt(attrs.get('seasonnumber', None)),
                episode_number = tryInt(attrs.get('episodenumber', None)),
                absolute_number = tryInt(attrs.get('absolute_number', None))
            )

            title = LibraryTitle(
                title = toUnicode(attrs.get('title')),
                simple_title = self.simplifyTitle(attrs.get('title')),
            )

            l.titles.append(title)

            db.add(l)
            db.commit()

        # Update library info
        if update_after is not False:
            handle = fireEventAsync if update_after is 'async' else fireEvent
            handle('library.update.episode', identifier = l.identifier, default_title = toUnicode(attrs.get('title', '')))

        library_dict = l.to_dict(self.default_dict)

        db.expire_all()
        return library_dict

    def update(self, identifier, default_title = '', force = False):

        if self.shuttingDown():
            return

        db = get_session()
        library = db.query(EpisodeLibrary).filter_by(identifier = identifier).first()
        done_status = fireEvent('status.get', 'done', single = True)

        if library:
            library_dict = library.to_dict(self.default_dict)

        do_update = True

        parent_identifier =  None
        if library.parent is not None:
            parent_identifier =  library.parent.identifier

        if library.status_id == done_status.get('id') and not force:
            do_update = False

        episode_params = {'season_identifier':  parent_identifier,
                          'episode_identifier': identifier,
                          'episode': library.episode_number,
                          'absolute':  library.absolute_number,}
        info = fireEvent('episode.info', merge = True, params = episode_params)

        # Don't need those here
        try: del info['in_wanted']
        except: pass
        try: del info['in_library']
        except: pass

        if not info or len(info) == 0:
            log.error('Could not update, no movie info to work with: %s', identifier)
            return False

        # Main info
        if do_update:
            library.plot = toUnicode(info.get('plot', ''))
            library.tagline = toUnicode(info.get('tagline', ''))
            library.year = info.get('year', 0)
            library.status_id = done_status.get('id')
            library.season_number = tryInt(info.get('seasonnumber', None))
            library.episode_number = tryInt(info.get('episodenumber', None))
            library.absolute_number = tryInt(info.get('absolute_number', None))
            try:
                library.last_updated = int(info.get('lastupdated'))
            except:
                library.last_updated = int(time.time())
            library.info.update(info)
            db.commit()

            # Titles
            [db.delete(title) for title in library.titles]
            db.commit()

            titles = info.get('titles', [])
            log.debug('Adding titles: %s', titles)
            counter = 0
            for title in titles:
                if not title:
                    continue
                title = toUnicode(title)
                t = LibraryTitle(
                    title = title,
                    simple_title = self.simplifyTitle(title),
                    default = (len(default_title) == 0 and counter == 0) or len(titles) == 1 or title.lower() == toUnicode(default_title.lower()) or (toUnicode(default_title) == u'' and toUnicode(titles[0]) == title)
                )
                library.titles.append(t)
                counter += 1

            db.commit()

            # Files
            images = info.get('images', [])
            for image_type in ['poster']:
                for image in images.get(image_type, []):
                    if not isinstance(image, (str, unicode)):
                        continue

                    file_path = fireEvent('file.download', url = image, single = True)
                    if file_path:
                        file_obj = fireEvent('file.add', path = file_path, type_tuple = ('image', image_type), single = True)
                        try:
                            file_obj = db.query(File).filter_by(id = file_obj.get('id')).one()
                            library.files.append(file_obj)
                            db.commit()

                            break
                        except:
                            log.debug('Failed to attach to library: %s', traceback.format_exc())

        library_dict = library.to_dict(self.default_dict)
        db.expire_all()
        return library_dict

    def updateReleaseDate(self, identifier):
        '''XXX:  Not sure what this is for yet in relation to an episode'''
        pass
        #db = get_session()
        #library = db.query(EpisodeLibrary).filter_by(identifier = identifier).first()

        #if not library.info:
            #library_dict = self.update(identifier, force = True)
            #dates = library_dict.get('info', {}).get('release_date')
        #else:
            #dates = library.info.get('release_date')

        #if dates and dates.get('expires', 0) < time.time() or not dates:
            #dates = fireEvent('movie.release_date', identifier = identifier, merge = True)
            #library.info.update({'release_date': dates })
            #db.commit()

        #db.expire_all()
        #return dates


    #TODO: Add to base class
    def simplifyTitle(self, title):

        title = toUnicode(title)

        nr_prefix = '' if title[0] in ascii_letters else '#'
        title = simplifyString(title)

        for prefix in ['the ']:
            if prefix == title[:len(prefix)]:
                title = title[len(prefix):]
                break

        return nr_prefix + title
