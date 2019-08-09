# -*- coding: utf-8 -*-

#################################################################################################

import json
import logging
import sys
import urlparse
import urllib
import os
import sys

import xbmc
import xbmcvfs
import xbmcgui
import xbmcplugin
import xbmcaddon
import routing

import client
import objects
import requests
import downloader
from emby import Emby
from database import reset, get_sync, Database, emby_db, get_credentials
from helper import _, event, settings, window, dialog, api, catch, kodi_version, JSONRPC

#################################################################################################

LOG = logging.getLogger("EMBY."+__name__)
EMBY = None
plugin = routing.Plugin()

#################################################################################################


def server():

    ''' Setup global Emby client value or raise an exception.
    '''
    def decorator(func):
        def wrapper(*args, **kwargs):
            global EMBY

            server = plugin.args.get('server')
            LOG.info(plugin.args)

            if server == 'None':
                server = plugin.args['server'] = None

            try:
                EMBY = Emby(server).get_client()
                if not EMBY['connected']:
                    raise AttributeError
            except (KeyError, AttributeError):

                LOG.warn("--[ ServerOffline ]")
                event('ServerConnect', {'Id': server})
                monitor = xbmc.Monitor()

                for i in range(30):
                    dialog("notification", heading="{emby}", icon="{emby}", message=_(33218), sound=False)

                    if server is None and window('emby_online.bool'):
                        Emby().set_state(window('emby.server.state.json'))

                        break

                    if server is not None and server in window('emby.server.states.json') or []:
                        Emby(server).set_state(window('emby.server.%s.state.json' % server))

                        break

                    if monitor.waitForAbort(1):
                        return
                else:
                    LOG.error("Server %s is not online", server)
                    dialog("notification", heading="{emby}", icon="{emby}", message=_(33146) if server is None else _(33149), sound=False)

                    return

                EMBY = Emby(server).get_client()

            return func(*args, **kwargs)

        return wrapper
    return decorator

def directory(label, path, folder=True, artwork=None, fanart=None, context=None):

    ''' Add directory listitem. context should be a list of tuples [(label, action)*]
    '''
    li = dir_listitem(label, path, artwork, fanart)

    if context:
        li.addContextMenuItems(context)

    xbmcplugin.addDirectoryItem(int(sys.argv[1]), path, li, folder)

    return li

def dir_listitem(label, path, artwork=None, fanart=None):

    li = xbmcgui.ListItem(label, path=path)
    li.setArt({'thumb': artwork or "special://home/addons/plugin.video.emby/icon.png"})
    li.setArt({"fanart": fanart or "special://home/addons/plugin.video.emby/fanart.jpg"})
    li.setArt({"landscape": artwork or fanart or "special://home/addons/plugin.video.emby/fanart.jpg"})

    return li


class Events(object):


    def __init__(self):

        ''' Parse the parameters. Reroute to our service.py
            where user is fully identified already.
        '''
        base_url = sys.argv[0]
        path = sys.argv[2]

        try:
            params = dict(urlparse.parse_qsl(path[1:]))
        except Exception:
            params = {}

        mode = params.get('mode')
        server = params.get('server')

        if server == 'None' or not server:
            server = None

        LOG.warn("path: %s params: %s", path, json.dumps(params, indent=4))
        LOG.warn(sys.argv)
        try:
            plugin.run()

            return
        except Exception as error:
            LOG.error(error)
            pass

        if '/extrafanart' in base_url:

            emby_path = path[1:]
            emby_id = params.get('id')
            get_fanart(emby_id, emby_path, server)

        elif '/Extras' in base_url or '/VideoFiles' in base_url:

            emby_path = path[1:]
            emby_id = params.get('id')
            get_video_extras(emby_id, emby_path, server)

        elif mode == 'play':

            window('emby.sync.pause.bool', True)
            window('emby.playlist.plugin.bool', True)

            try:
                objects.PlayPlugin(params, server).play('trailer' in base_url)
            except Exception as error:
                LOG.exception(error)

                if not xbmc.Player().isPlaying():
                    xbmc.Player().stop()

                xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())

            window('emby.sync.pause.bool', clear=True)
            window('emby.playlist.plugin', clear=True)

        elif mode =='playstrm':

            window('emby.sync.pause.bool', True)
            window('emby.playlist.plugin.bool', True)

            while not window('emby.playlist.play.bool'):
                xbmc.sleep(50)

                if window('emby.playlist.aborted.bool'):
                    LOG.warn("[ playback aborted ]")

                    break
            else:
                LOG.info("[ playback started ]")
                xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())

            window('emby.playlist.aborted', clear=True)
            window('emby.sync.pause', clear=True)
            window('emby.playlist.plugin', clear=True)

        elif mode == 'playsingle':

            window('emby.sync.pause.bool', True)
            window('emby.playlist.plugin.bool', True)

            try:
                objects.PlaySingle(params, server).play()
            except Exception as error:
                LOG.exception(error)

                if not xbmc.Player().isPlaying():
                    xbmc.Player().stop()

                xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())

            window('emby.sync.pause', clear=True)
            window('emby.playlist.plugin', clear=True)

        if mode == 'playlist':
            event('PlayPlaylist', {'Id': params['id'], 'ServerId': server})
        elif mode == 'photoviewer':
            xbmc.executebuiltin('ShowPicture(%s/emby/Items/%s/Images/Primary)' % (Emby(server)['auth/server-address'], params['id']))
        elif mode == 'deviceid':
            client.reset_device_id()
        elif mode == 'reset':
            reset()
        elif mode == 'delete':
            delete_item()
        elif mode == 'nextepisodes':
            get_next_episodes(params['id'], params['limit'])
        elif mode == 'browse':
            browser(params.get('type'), params.get('id'), params.get('folder'), server)

@plugin.route('/photo/<item_id>')
@server()
def photo(item_id):
    xbmc.executebuiltin('ShowPicture(%s/emby/Items/%s/Images/Primary)' % (EMBY['auth/server-address'], item_id))


@plugin.route('/')
def root():

    ''' Display all emby nodes and dynamic entries when appropriate.
    '''
    LOG.info(plugin.args)
    total = int(window('Emby.nodes.total') or 0)
    sync = get_sync()
    whitelist = [x.replace('Mixed:', "") for x in sync['Whitelist']]
    servers = get_credentials()['Servers'][1:]

    for i in range(total):

        window_prop = "Emby.nodes.%s" % i
        path = window('%s.index' % window_prop)

        if not path:
            path = window('%s.content' % window_prop) or window('%s.path' % window_prop)

        label = window('%s.title' % window_prop)
        node = window('%s.type' % window_prop)
        artwork = window('%s.artwork' % window_prop)
        view_id = window('%s.id' % window_prop)
        context = []

        if view_id and node in ('movies', 'tvshows', 'musicvideos', 'music', 'mixed') and view_id not in whitelist:
            label = "%s %s" % (label.decode('utf-8'), _(33166))
            context.append((_(33123), "RunPlugin(%s)" % plugin.url_for(sync_library, library_id=view_id)))

        if view_id and node in ('movies', 'tvshows', 'musicvideos', 'music') and view_id in whitelist:

            context.append((_(33136), "RunPlugin(%s)" % plugin.url_for(update_library, library_id=view_id)))
            context.append((_(33132), "RunPlugin(%s)" % plugin.url_for(repair_library, library_id=view_id)))
            context.append((_(33133), "RunPlugin(%s)" % plugin.url_for(remove_library, library_id=view_id)))

        LOG.debug("--[ listing/%s/%s ] %s", node, label, path)

        if path:
            if xbmc.getCondVisibility('Window.IsActive(Pictures)') and node in ('photos', 'homevideos'):
                directory(label, path, artwork=artwork)
            elif xbmc.getCondVisibility('Window.IsActive(Videos)') and node not in ('photos', 'music', 'audiobooks'):
                LOG.info(label)
                LOG.info(path)
                directory(label, path, artwork=artwork, context=context)
            elif xbmc.getCondVisibility('Window.IsActive(Music)') and node in ('music'):
                directory(label, path, artwork=artwork, context=context)
            elif not xbmc.getCondVisibility('Window.IsActive(Videos) | Window.IsActive(Pictures) | Window.IsActive(Music)'):
                directory(label, path, artwork=artwork)

    for server in servers:
        context = [(_(30500), "RunPlugin(%s)" % plugin.url_for(server_ssl, server_id=server['Id']))]

        if server.get('ManualAddress'):
            context.append((_(33141), "RunPlugin(%s)" % plugin.url_for(server_remove, server_id=server['Id'])))

        if 'AccessToken' not in server:
            directory("%s (%s)" % (server['Name'], _(30539)), plugin.url_for(server_login, server_id=server['Id']), False, context=context)
        else:
            directory(server['Name'], "plugin://plugin.video.emby/?mode=browse&server=%s" % server['Id'], context=context)


    directory(_(33134), plugin.url_for(server_add), False)
    directory(_(33054), plugin.url_for(additional_users), False)
    directory(_(33194), plugin.url_for(manage_libraries), True)
    directory(_(5), plugin.url_for(addon_settings), False)
    directory(_(33059), plugin.url_for(sync_artwork), False)
    directory(_(33058), plugin.url_for(addon_reset), False)
    directory(_(33192), plugin.url_for(addon_restart), False)

    if settings('backupPath'):
        directory(_(33092), plugin.url_for(addon_backup), False)

    directory("Changelog", plugin.url_for(changelog), False)
    directory(_(33163), None, False, artwork="special://home/addons/plugin.video.emby/donations.png")

    xbmcplugin.setContent(int(sys.argv[1]), 'files')
    xbmcplugin.endOfDirectory(int(sys.argv[1]))


@plugin.route('/manage')
def manage_libraries():

    directory(_(33098), plugin.url_for(sync_boxsets), False)
    directory(_(33154), plugin.url_for(sync_libraries), False)
    directory(_(33139), plugin.url_for(update_libraries), False)
    directory(_(33140), plugin.url_for(repair_libraries), False)
    directory(_(33184), plugin.url_for(remove_libraries), False)
    directory(_(33060), plugin.url_for(sync_themes), False)

    if kodi_version() >= 18:
        directory(_(33202), plugin.url_for(sync_music_patch), False)

    xbmcplugin.setContent(int(sys.argv[1]), 'files')
    xbmcplugin.endOfDirectory(int(sys.argv[1]))


@plugin.route('/embyconnect')
def emby_connect():
    event('EmbyConnect')


@plugin.route('/settings')
def addon_settings():
    xbmc.executebuiltin('Addon.OpenSettings(plugin.video.emby)')


@plugin.route('/patch')
def patch_update():
    event('CheckUpdate')


@plugin.route('/server')
def server_add():
    event('AddServer')


@plugin.route('/server/update')
def server_update():
    event('UpdateServer')


@plugin.route('/server/<server_id>/login')
def server_login(server_id):
    event('ServerConnect', {'Id': server_id})


@plugin.route('/server/<server_id>/ssl')
def server_ssl(server_id):
    event('SetServerSSL', {'Id': server_id})


@plugin.route('/server/<server_id>/remove')
def server_remove(server_id):
    event('RemoveServer', {'Id': server_id})


@plugin.route('/delete')
@server()
def delete_item():
    import context

    ''' Delete keymap action.
    '''
    context.Context(delete=True)


@plugin.route('/reset')
def addon_reset():
    reset()


@plugin.route('/reset/deviceid')
def reset_device_id():
    client.reset_device_id()


@plugin.route('/reset/patch')
def patch_reset():
    event('ResetUpdate')


@plugin.route('/restart')
def addon_restart():
    window('emby.restart.bool', True)


@plugin.route('/backup')
def addon_backup():
    from helper.utils import delete_folder, copytree

    ''' Emby backup of settings.xml and database files.
    '''
    path = settings('backupPath')
    folder_name = "Kodi%s.%s" % (xbmc.getInfoLabel('System.BuildVersion')[:2], xbmc.getInfoLabel('System.Date(dd-mm-yy)'))
    folder_name = dialog("input", heading=_(33089), defaultt=folder_name)

    if not folder_name:
        return

    backup = os.path.join(path, folder_name)

    if xbmcvfs.exists(backup + '/'):
        if not dialog("yesno", heading="{emby}", line1=_(33090)):

            return backup()

        delete_folder(backup)

    addon_data = xbmc.translatePath("special://profile/addon_data/plugin.video.emby").decode('utf-8')
    destination_data = os.path.join(backup, "addon_data", "plugin.video.emby")
    destination_databases = os.path.join(backup, "Database")

    if not xbmcvfs.mkdirs(path) or not xbmcvfs.mkdirs(destination_databases):

        LOG.info("Unable to create all directories")
        dialog("notification", heading="{emby}", icon="{emby}", message=_(33165), sound=False)

        return

    copytree(addon_data, destination_data)

    databases = objects.Objects().objects

    db = xbmc.translatePath(databases['emby']).decode('utf-8')
    xbmcvfs.copy(db, os.path.join(destination_databases, db.rsplit('\\', 1)[1]))
    LOG.info("copied emby.db")

    db = xbmc.translatePath(databases['video']).decode('utf-8')
    filename = db.rsplit('\\', 1)[1]
    xbmcvfs.copy(db, os.path.join(destination_databases, filename))
    LOG.info("copied %s", filename)

    if settings('enableMusic.bool'):

        db = xbmc.translatePath(databases['music']).decode('utf-8')
        filename = db.rsplit('\\', 1)[1]
        xbmcvfs.copy(db, os.path.join(destination_databases, filename))
        LOG.info("copied %s", filename)

    LOG.info("backup completed")
    dialog("ok", heading="{emby}", line1="%s %s" % (_(33091), backup))


@plugin.route('/changelog')
def changelog(version=False):
    
    ''' Display the changelog for the current version or the latest release.
    '''
    version = client.get_version()
    resp = int(bool(version)) or dialog("select", heading="{emby}", list=[_(33212), version])

    if resp < 0:
        return

    resp = "tags/%s" % version if resp else "latest"
    src = "https://api.github.com/repos/MediaBrowser/plugin.video.emby/releases/%s" % resp

    try:
        response = requests.get(src)
        response.raise_for_status()
        response.encoding = 'utf-8'
        response = response.json()
        response['body'] = "[B]%s[/B]\n\n%s" % (response['name'], response['body'])
        response['body'] = response['body'].replace('**:', '[/B]').replace('**', '[B]').replace('*', '-')
        dialog("textviewer", heading="{emby}", text=response['body'])
    except Exception as error:

        LOG.error(error)
        dialog("notification", heading="{emby}", message=_(33204), icon="{emby}", time=1000, sound=False)

    return


@plugin.route('/changelog/version')
def changelog_version():
    changelog(version=True)


@plugin.route('/sync/library')
@server()
def sync_libraries():
    event('AddLibrarySelection')


@plugin.route('/sync/library/repair')
@server()
def repair_libraries():
    event('RepairLibrarySelection')


@plugin.route('/sync/library/update')
@server()
def update_libraries():
    event('SyncLibrarySelection')


@plugin.route('/sync/library/remove')
@server()
def remove_libraries():
    event('RemoveLibrarySelection')


@plugin.route('/sync/library/<library_id>')
@server()
def sync_library(library_id):
    event('SyncLibrary', {'Id': library_id})


@plugin.route('/sync/library/<library_id>/update')
@server()
def update_library(library_id):
    event('SyncLibrary', {'Id': library_id, 'Update': True})


@plugin.route('/sync/library/<library_id>/repair')
@server()
def repair_library(library_id):
    event('RepairLibrary', {'Id': library_id})


@plugin.route('/sync/library/<library_id>/remove')
@server()
def remove_library(library_id):
    event('RemoveLibrary', {'Id': library_id})


@plugin.route('/sync/artwork')
@server()
def sync_artwork():
    from objects.kodi import artwork

    ''' Give the option to delete all artwork and cache all artwork found in Kodi.
    '''
    artwork.Artwork(None).cache_textures()


@plugin.route('/sync/music/patch')
@server()
def sync_music_patch():
    event('PatchMusic', {'Notification': True})


@plugin.route('/sync/boxsets')
@server()
def sync_boxsets():
    event('SyncLibrary', {'Id': "Boxsets:Refresh"})


@plugin.route('/sync/themes')
@server()
def sync_themes():
    from helper.utils import normalize_string
    from helper.playutils import PlayUtils
    from helper.xmls import tvtunes_nfo

    ''' Add theme media locally, via strm. This is only for tv tunes.
        If another script is used, adjust this code.
    '''
    library = xbmc.translatePath("special://profile/addon_data/plugin.video.emby/library").decode('utf-8')
    play = settings('useDirectPaths') == "1"

    if not xbmcvfs.exists(library + '/'):
        xbmcvfs.mkdir(library)

    if xbmc.getCondVisibility('System.HasAddon(script.tvtunes)'):

        tvtunes = xbmcaddon.Addon(id="script.tvtunes")
        tvtunes.setSetting('custom_path_enable', "true")
        tvtunes.setSetting('custom_path', library)
        LOG.info("TV Tunes custom path is enabled and set.")
    else:
        dialog("ok", heading="{emby}", line1=_(33152))

        return

    with Database('emby') as embydb:
        all_views = emby_db.EmbyDatabase(embydb.cursor).get_views()
        views = [x[0] for x in all_views if x[2] in ('movies', 'tvshows', 'mixed')]

    items = {}
    server = EMBY['auth/server-address']
    token = EMBY['auth/token']

    for view in views:
        result = EMBY['api'].get_items_theme_video(view)

        for item in result['Items']:

            folder = normalize_string(item['Name'].encode('utf-8'))
            items[item['Id']] = folder

        result = EMBY['api'].get_items_theme_song(view)

        for item in result['Items']:

            folder = normalize_string(item['Name'].encode('utf-8'))
            items[item['Id']] = folder

    for item in items:

        nfo_path = os.path.join(library, items[item]).decode('utf-8')
        nfo_file = os.path.join(nfo_path, "tvtunes.nfo").decode('utf-8')

        if not xbmcvfs.exists(nfo_path):
            xbmcvfs.mkdir(nfo_path)

        themes = EMBY['api'].get_themes(item)
        paths = []

        for theme in themes['ThemeVideosResult']['Items'] + themes['ThemeSongsResult']['Items']:
            putils = PlayUtils(theme, False, EMBY)

            if play:
                paths.append(putils.direct_play(theme['MediaSources'][0]))
            else:
                paths.append(putils.direct_url(theme['MediaSources'][0]))

        tvtunes_nfo(nfo_file, paths)

    dialog("notification", heading="{emby}", message=_(33153), icon="{emby}", time=1000, sound=False)


@plugin.route('/users')
@server()
def additional_users():

    permanent = plugin.args.get('permanent') == 'true'
    session = EMBY['api'].get_device(EMBY['config/app.device_id'])
    hidden = None if settings('addUsersHidden.bool') else False
    users = EMBY['api'].get_users(False, hidden)

    for user in users:

        if user['Id'] == session[0]['UserId']:
            users.remove(user)

            break

    while True:

        session = EMBY['api'].get_device(EMBY['config/app.device_id'])
        additional = current = session[0]['AdditionalUsers']
        add_session = True

        if permanent:

            perm_users = settings('addUsers').split(',') if settings('addUsers') else []
            current = []

            for user in users:
                for perm_user in perm_users:

                    if user['Id'] == perm_user:
                        current.append({'UserName': user['Name'], 'UserId': user['Id']})

        result = dialog("select", _(33061), [_(33062), _(33063)] if current else [_(33062)])

        if result < 0:
            break

        if not result: # Add user

            eligible = [x for x in users if x['Id'] not in [current_user['UserId'] for current_user in current]]
            resp = dialog("select", _(33064), [x['Name'] for x in eligible])

            if resp < 0:
                break

            user = eligible[resp]

            if permanent:

                perm_users.append(user['Id'])
                settings('addUsers', ','.join(perm_users))

                if user['Id'] in [current_user['UserId'] for current_user in additional]:
                    add_session = False
            
            if add_session:
                event('AddUser', {'Id': user['Id'], 'Add': True})

            dialog("notification", heading="{emby}", message="%s %s" % (_(33067), user['Name']), icon="{emby}", time=1000, sound=False)
        else: # Remove user
            resp = dialog("select", _(33064), [x['UserName'] for x in current])

            if resp < 0:
                break

            user = current[resp]

            if permanent:

                perm_users.remove(user['UserId'])
                settings('addUsers', ','.join(perm_users))
            
            if add_session:
                event('AddUser', {'Id': user['UserId'], 'Add': False})

            dialog("notification", heading="{emby}", message="%s %s" % (_(33066), user['UserName']), icon="{emby}", time=1000, sound=False)


@plugin.route('/browse/<folder>')
@server()
def browse(folder):

    media = plugin.args.get('type')
    item_id = plugin.args.get('view', folder)
    server_id = EMBY['auth/server-id'] if not EMBY['config/app.default'] else None

    ''' Browse content dynamically.
    '''
    """
    LOG.info("--[ v:%s/%s ] %s", view_id, media, folder)
    LOG.info("hello")
    server_id = EMBY['auth/server-id'] if server_id else None
    LOG.info(server_id)
    """
    folder = folder.lower() if folder else None

    if folder is None and media in ('homevideos', 'movies', 'books', 'audiobooks'):
        LOG.info("hello4")
        return browse_subfolders(media, view_id, plugin.args.get('server'))
    LOG.info("hello1")
    if folder and folder == 'firstletter':
        return browse_letters(media, view_id, server_id)
    LOG.info("hello2")
    if view_id:

        view = EMBY['api'].get_item(view_id)
        xbmcplugin.setPluginCategory(int(sys.argv[1]), view['Name'])
    LOG.info("hello3")
    content_type = "files"

    if media in ('tvshows', 'seasons', 'episodes', 'movies', 'musicvideos', 'songs', 'albums'):
        content_type = media
    elif media in ('homevideos', 'photos'):
        content_type = "images"
    elif media in ('books', 'audiobooks'):
        content_type = "videos"
    elif media == 'music':
        content_type = "artists"


    if folder == 'recentlyadded':
        listing = EMBY['api'].get_recently_added(None, view_id, None)
    elif folder == 'genres':
        listing = EMBY['api'].get_genres(view_id)
    elif media == 'livetv':
        listing = EMBY['api'].get_channels()
    elif folder == 'unwatched':
        listing = downloader.get_filtered_section(view_id, None, None, None, None, None, ['IsUnplayed'], None, server_id)
    elif folder == 'favorite':
        listing = downloader.get_filtered_section(view_id, None, None, None, None, None, ['IsFavorite'], None, server_id)
    elif folder == 'inprogress':
        listing = downloader.get_filtered_section(view_id, None, None, None, None, None, ['IsResumable'], None, server_id)
    elif folder == 'boxsets':
        listing = downloader.get_filtered_section(view_id, get_media_type('boxsets'), None, True, None, None, None, None, server_id)
    elif folder == 'random':
        listing = downloader.get_filtered_section(view_id, get_media_type(content_type), 25, True, "Random", None, None, None, server_id)
    elif (folder or "").startswith('firstletter-'):
        listing = downloader.get_filtered_section(view_id, get_media_type(content_type), None, None, None, None, None, {'NameStartsWith': folder.split('-')[1]}, server_id)
    elif (folder or "").startswith('genres-'):
        listing = downloader.get_filtered_section(view_id, get_media_type(content_type), None, None, None, None, None, {'GenreIds': folder.split('-')[1]}, server_id)
    elif folder == 'favepisodes':
        listing = downloader.get_filtered_section(None, get_media_type(content_type), 25, None, None, None, ['IsFavorite'], None, server_id)
    elif media == 'homevideos':
        listing = downloader.get_filtered_section(folder or view_id, get_media_type(content_type), None, False, None, None, None, None, server_id)
    elif media == 'movies':
        listing = downloader.get_filtered_section(folder or view_id, get_media_type(content_type), None, True, None, None, None, None, server_id)
    elif media in ('boxset', 'library'):
        listing = downloader.get_filtered_section(folder or view_id, None, None, True, None, None, None, None, server_id)
    elif media == 'episodes':
        listing = downloader.get_filtered_section(folder or view_id, get_media_type(content_type), None, True, None, None, None, None, server_id)
    elif media == 'boxsets':
        listing = downloader.get_filtered_section(folder or view_id, None, None, False, None, None, ['Boxsets'], None, server_id)
    elif media == 'tvshows':
        LOG.info("hello world")
        listing = downloader.get_filtered_section(folder or view_id, get_media_type(content_type), None, True, None, None, None, None, server_id)
    elif media == 'seasons':
        listing = EMBY['api'].get_seasons(folder)
    elif media != 'files':
        listing = downloader.get_filtered_section(folder or view_id, get_media_type(content_type), None, False, None, None, None, None, server_id)
    else:
        listing = downloader.get_filtered_section(folder or view_id, None, None, False, None, None, None, None, server_id)

    LOG.info(listing)
    if listing:

        listitems = objects.ListItem(EMBY['auth/server-address'])
        list_li = []
        listing = listing if type(listing) == list else listing.get('Items', [])

        for item in listing:

            li = xbmcgui.ListItem()
            li.setProperty('embyid', item['Id'])
            li.setProperty('embyserver', server_id)
            li = listitems.set(item, li)

            if item.get('IsFolder'):

                params = {
                    'id': view_id or item['Id'],
                    'mode': "browse",
                    'type': get_folder_type(item, media) or media,
                    'folder': item['Id'],
                    'server': server_id
                }
                path = "%s?%s" % ("plugin://plugin.video.emby/",  urllib.urlencode(params))
                context = []

                if item['Type'] in ('Series', 'Season', 'Playlist'):
                    context.append(("Play", "RunPlugin(plugin://plugin.video.emby/?mode=playlist&id=%s&server=%s)" % (item['Id'], server_id)))

                if item['UserData']['Played']:
                    context.append((_(16104), "RunPlugin(plugin://plugin.video.emby/?mode=unwatched&id=%s&server=%s)" % (item['Id'], server_id)))
                else:
                    context.append((_(16103), "RunPlugin(plugin://plugin.video.emby/?mode=watched&id=%s&server=%s)" % (item['Id'], server_id)))

                li.addContextMenuItems(context)
                list_li.append((path, li, True))

            elif item['Type'] == 'Genre':

                params = {
                    'id': view_id or item['Id'],
                    'mode': "browse",
                    'type': get_folder_type(item, media) or media,
                    'folder': 'genres-%s' % item['Id'],
                    'server': server_id
                }
                path = "%s?%s" % ("plugin://plugin.video.emby/",  urllib.urlencode(params))
                list_li.append((path, li, True))

            else:
                if item['Type'] == 'Photo':
                    path = plugin.url_for(photo, item_id=item['Id'])

                elif item['Type'] != 'PhotoAlbum':

                    if kodi_version() > 17:
                        path = "http://127.0.0.1:57578/emby/play/file.strm?mode=play&Id=%s&server=%s" % (item['Id'], server_id)
                    else:
                        params = {
                            'id': item['Id'],
                            'mode': "play",
                            'server': server_id
                        }
                        path = "%s?%s" % ("plugin://plugin.video.emby/", urllib.urlencode(params))
                    
                    li.setProperty('path', path)
                    context = [(_(13412), "RunPlugin(plugin://plugin.video.emby/?mode=playlist&id=%s&server=%s)" % (item['Id'], server_id))]

                    if item['UserData']['Played']:
                        context.append((_(16104), "RunPlugin(plugin://plugin.video.emby/?mode=unwatched&id=%s&server=%s)" % (item['Id'], server_id)))
                    else:
                        context.append((_(16103), "RunPlugin(plugin://plugin.video.emby/?mode=watched&id=%s&server=%s)" % (item['Id'], server_id)))

                    li.addContextMenuItems(context)

                list_li.append((li.getProperty('path'), li, False))

        xbmcplugin.addDirectoryItems(int(sys.argv[1]), list_li, len(list_li))

    if content_type == 'images':
        xbmcplugin.addSortMethod(int(sys.argv[1]), xbmcplugin.SORT_METHOD_VIDEO_TITLE)
        xbmcplugin.addSortMethod(int(sys.argv[1]), xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(int(sys.argv[1]), xbmcplugin.SORT_METHOD_DATE)
        xbmcplugin.addSortMethod(int(sys.argv[1]), xbmcplugin.SORT_METHOD_VIDEO_RATING)
        xbmcplugin.addSortMethod(int(sys.argv[1]), xbmcplugin.SORT_METHOD_VIDEO_RUNTIME)

    xbmcplugin.setContent(int(sys.argv[1]), content_type)
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

@catch()
def browse_subfolders(media, view_id, server_id=None):

    ''' Display submenus for emby views.
    '''
    LOG.info("hello5")
    from views import DYNNODES
    LOG.info("hello6")
    view = EMBY['api'].get_item(view_id)
    xbmcplugin.setPluginCategory(plugin.handle, view['Name'])
    nodes = DYNNODES[media]
    LOG.info("hello7")
    for node in nodes:

        params = {
            'id': view_id,
            'mode': "browse",
            'type': media,
            'folder': view_id if node[0] == 'all' else node[0],
            'server': server_id
        }
        path = "%s?%s" % ("plugin://plugin.video.emby/",  urllib.urlencode(params))
        directory(node[1] or view['Name'], path)
    LOG.info("hello8")
    xbmcplugin.setContent(plugin.handle, 'files')
    xbmcplugin.endOfDirectory(plugin.handle)

def browse_letters(media, view_id, server_id=None):

    ''' Display letters as options.
    '''
    letters = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    view = EMBY['api'].get_item(view_id)
    xbmcplugin.setPluginCategory(int(sys.argv[1]), view['Name'])

    for node in letters:

        params = {
            'id': view_id,
            'mode': "browse",
            'type': media,
            'folder': 'firstletter-%s' % node,
            'server': server_id
        }
        path = "%s?%s" % ("plugin://plugin.video.emby/",  urllib.urlencode(params))
        directory(node, path)

    xbmcplugin.setContent(int(sys.argv[1]), 'files')
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def get_folder_type(item, content_type=None):

    media = item['Type']

    if media == 'Series':
        return "seasons"
    elif media == 'Season':
        return "episodes"
    elif media == 'BoxSet':
        return "boxset"
    elif media == 'MusicArtist':
        return "albums"
    elif media == 'MusicAlbum':
        return "songs"
    elif media == 'CollectionFolder':
        return item.get('CollectionType', 'library')
    elif media == 'Folder' and content_type == 'music':
        return "albums"


def get_media_type(media):

    if media == 'movies':
        return "Movie,BoxSet"
    elif media == 'homevideos':
        return "Video,Folder,PhotoAlbum,Photo"
    elif media == 'episodes':
        return "Episode"
    elif media == 'boxsets':
        return "BoxSet"
    elif media == 'tvshows':
        return "Series"
    elif media == 'music':
        return "MusicArtist,MusicAlbum,Audio"

@server()
def get_fanart(item_id, path, server_id=None):

    ''' Get extra fanart for listitems. This is called by skinhelper.
        Images are stored locally, due to the Kodi caching system.
    '''
    if not item_id and 'plugin.video.emby' in path:
        item_id = path.split('/')[-2]

    if not item_id:
        return

    LOG.info("[ extra fanart ] %s", item_id)
    objects = objects.Objects()
    list_li = []
    directory = xbmc.translatePath("special://thumbnails/emby/%s/" % item_id).decode('utf-8')

    if not xbmcvfs.exists(directory):

        xbmcvfs.mkdirs(directory)
        item = EMBY['api'].get_item(item_id)
        obj = objects.map(item, 'Artwork')
        backdrops = api.API(item, EMBY['auth/server-address']).get_all_artwork(obj)
        tags = obj['BackdropTags']

        for index, backdrop in enumerate(backdrops):

            tag = tags[index]
            fanart = os.path.join(directory, "fanart%s.jpg" % tag)
            li = xbmcgui.ListItem(tag, path=fanart)
            xbmcvfs.copy(backdrop, fanart)
            list_li.append((fanart, li, False))
    else:
        LOG.debug("cached backdrop found")
        dirs, files = xbmcvfs.listdir(directory)

        for file in files:
            fanart = os.path.join(directory, file.decode('utf-8'))
            li = xbmcgui.ListItem(file, path=fanart)
            list_li.append((fanart, li, False))

    xbmcplugin.addDirectoryItems(int(sys.argv[1]), list_li, len(list_li))
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

@server()
def get_video_extras(item_id, path, server_id=None):

    ''' Returns the video files for the item as plugin listing, can be used
        to browse actual files or video extras, etc.
    '''
    if not item_id and 'plugin.video.emby' in path:
        item_id = path.split('/')[-2]

    if not item_id:
        return

    item = EMBY['api'].get_item(item_id)
    # TODO

    """
    def getVideoFiles(embyId,embyPath):
        #returns the video files for the item as plugin listing, can be used for browsing the actual files or videoextras etc.
        emby = embyserver.Read_EmbyServer()
        if not embyId:
            if "plugin.video.emby" in embyPath:
                embyId = embyPath.split("/")[-2]
        if embyId:
            item = emby.getItem(embyId)
            putils = playutils.PlayUtils(item)
            if putils.isDirectPlay():
                #only proceed if we can access the files directly. TODO: copy local on the fly if accessed outside
                filelocation = putils.directPlay()
                if not filelocation.endswith("/"):
                    filelocation = filelocation.rpartition("/")[0]
                dirs, files = xbmcvfs.listdir(filelocation)
                for file in files:
                    file = filelocation + file
                    li = xbmcgui.ListItem(file, path=file)
                    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=file, listitem=li)
                for dir in dirs:
                    dir = filelocation + dir
                    li = xbmcgui.ListItem(dir, path=dir)
                    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=dir, listitem=li, isFolder=True)
        #xbmcplugin.endOfDirectory(int(sys.argv[1]))
    """

def get_next_episodes(item_id, limit):

    ''' Only for synced content.
    '''
    with Database('emby') as embydb:

        db = emby_db.EmbyDatabase(embydb.cursor)
        library = db.get_view_name(item_id)

        if not library:
            return

    result = JSONRPC('VideoLibrary.GetTVShows').execute({
                'sort': {'order': "descending", 'method': "lastplayed"},
                'filter': {
                    'and': [
                        {'operator': "true", 'field': "inprogress", 'value': ""},
                        {'operator': "is", 'field': "tag", 'value': "%s" % library}
                    ]},
                'properties': ['title', 'studio', 'mpaa', 'file', 'art']
             })

    try:
        items = result['result']['tvshows']
    except (KeyError, TypeError):
        return

    list_li = []

    for item in items:
        if settings('ignoreSpecialsNextEpisodes.bool'):
            params = {
                'tvshowid': item['tvshowid'],
                'sort': {'method': "episode"},
                'filter': {
                    'and': [
                        {'operator': "lessthan", 'field': "playcount", 'value': "1"},
                        {'operator': "greaterthan", 'field': "season", 'value': "0"}
                ]},
                'properties': [
                    "title", "playcount", "season", "episode", "showtitle",
                    "plot", "file", "rating", "resume", "tvshowid", "art",
                    "streamdetails", "firstaired", "runtime", "writer",
                    "dateadded", "lastplayed"
                ],
                'limits': {"end": 1}
            }
        else:
            params = {
                'tvshowid': item['tvshowid'],
                'sort': {'method': "episode"},
                'filter': {'operator': "lessthan", 'field': "playcount", 'value': "1"},
                'properties': [
                    "title", "playcount", "season", "episode", "showtitle",
                    "plot", "file", "rating", "resume", "tvshowid", "art",
                    "streamdetails", "firstaired", "runtime", "writer",
                    "dateadded", "lastplayed"
                ],
                'limits': {"end": 1}
            }

        result = JSONRPC('VideoLibrary.GetEpisodes').execute(params)

        try:
            episodes = result['result']['episodes']
        except (KeyError, TypeError):
            pass
        else:
            for episode in episodes:

                li = create_listitem(episode)
                list_li.append((episode['file'], li))

        if len(list_li) == limit:
            break

    xbmcplugin.addDirectoryItems(int(sys.argv[1]), list_li, len(list_li))
    xbmcplugin.setContent(int(sys.argv[1]), 'episodes')
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def create_listitem(item):

    ''' Listitem based on jsonrpc items.
    '''
    title = item['title']
    label2 = ""
    li = xbmcgui.ListItem(title)
    li.setProperty('IsPlayable', "true")

    metadata = {
        'Title': title,
        'duration': str(item['runtime']/60),
        'Plot': item['plot'],
        'Playcount': item['playcount']
    }

    if "showtitle" in item:
        metadata['TVshowTitle'] = item['showtitle']
        label2 = item['showtitle']

    if "episodeid" in item:
        # Listitem of episode
        metadata['mediatype'] = "episode"
        metadata['dbid'] = item['episodeid']

    # TODO: Review once Krypton is RC - probably no longer needed if there's dbid
    if "episode" in item:
        episode = item['episode']
        metadata['Episode'] = episode

    if "season" in item:
        season = item['season']
        metadata['Season'] = season

    if season and episode:
        episodeno = "s%.2de%.2d" % (season, episode)
        li.setProperty('episodeno', episodeno)
        label2 = "%s - %s" % (label2, episodeno) if label2 else episodeno

    if "firstaired" in item:
        metadata['Premiered'] = item['firstaired']

    if "rating" in item:
        metadata['Rating'] = str(round(float(item['rating']),1))

    if "director" in item:
        metadata['Director'] = " / ".join(item['director'])

    if "writer" in item:
        metadata['Writer'] = " / ".join(item['writer'])

    if "cast" in item:
        cast = []
        castandrole = []
        for person in item['cast']:
            name = person['name']
            cast.append(name)
            castandrole.append((name, person['role']))
        metadata['Cast'] = cast
        metadata['CastAndRole'] = castandrole

    li.setLabel2(label2)
    li.setInfo(type="Video", infoLabels=metadata)
    li.setProperty('resumetime', str(item['resume']['position']))
    li.setProperty('totaltime', str(item['resume']['total']))
    li.setArt(item['art'])
    li.setThumbnailImage(item['art'].get('thumb',''))
    li.setIconImage('DefaultTVShows.png')
    li.setProperty('dbid', str(item['episodeid']))
    li.setProperty('fanart_image', item['art'].get('tvshow.fanart',''))

    for key, value in item['streamdetails'].iteritems():
        for stream in value:
            li.addStreamInfo(key, stream)

    return li

def add_user(permanent=False):

    ''' Add or remove users from the default server session.
        permanent=True from the add-on settings.
    '''
    if not window('emby_online.bool'):
        return

    get_server()
    session = EMBY['api'].get_device(EMBY['config/app.device_id'])
    hidden = None if settings('addUsersHidden.bool') else False
    users = EMBY['api'].get_users(False, hidden)

    for user in users:

        if user['Id'] == session[0]['UserId']:
            users.remove(user)

            break

    while True:

        session = EMBY['api'].get_device(EMBY['config/app.device_id'])
        additional = current = session[0]['AdditionalUsers']
        add_session = True

        if permanent:

            perm_users = settings('addUsers').split(',') if settings('addUsers') else []
            current = []

            for user in users:
                for perm_user in perm_users:

                    if user['Id'] == perm_user:
                        current.append({'UserName': user['Name'], 'UserId': user['Id']})

        result = dialog("select", _(33061), [_(33062), _(33063)] if current else [_(33062)])

        if result < 0:
            break

        if not result: # Add user

            eligible = [x for x in users if x['Id'] not in [current_user['UserId'] for current_user in current]]
            resp = dialog("select", _(33064), [x['Name'] for x in eligible])

            if resp < 0:
                break

            user = eligible[resp]

            if permanent:

                perm_users.append(user['Id'])
                settings('addUsers', ','.join(perm_users))

                if user['Id'] in [current_user['UserId'] for current_user in additional]:
                    add_session = False
            
            if add_session:
                event('AddUser', {'Id': user['Id'], 'Add': True})

            dialog("notification", heading="{emby}", message="%s %s" % (_(33067), user['Name']), icon="{emby}", time=1000, sound=False)
        else: # Remove user
            resp = dialog("select", _(33064), [x['UserName'] for x in current])

            if resp < 0:
                break

            user = current[resp]

            if permanent:

                perm_users.remove(user['UserId'])
                settings('addUsers', ','.join(perm_users))
            
            if add_session:
                event('AddUser', {'Id': user['UserId'], 'Add': False})

            dialog("notification", heading="{emby}", message="%s %s" % (_(33066), user['UserName']), icon="{emby}", time=1000, sound=False)

def get_themes():

    ''' Add theme media locally, via strm. This is only for tv tunes.
        If another script is used, adjust this code.
    '''
    from helper.utils import normalize_string
    from helper.playutils import PlayUtils
    from helper.xmls import tvtunes_nfo

    library = xbmc.translatePath("special://profile/addon_data/plugin.video.emby/library").decode('utf-8')
    play = settings('useDirectPaths') == "1"

    if not xbmcvfs.exists(library + '/'):
        xbmcvfs.mkdir(library)

    if xbmc.getCondVisibility('System.HasAddon(script.tvtunes)'):

        tvtunes = xbmcaddon.Addon(id="script.tvtunes")
        tvtunes.setSetting('custom_path_enable', "true")
        tvtunes.setSetting('custom_path', library)
        LOG.info("TV Tunes custom path is enabled and set.")

    elif xbmc.getCondVisibility('System.HasAddon(service.tvtunes)'):

        tvtunes = xbmcaddon.Addon(id="service.tvtunes")
        tvtunes.setSetting('custom_path_enable', "true")
        tvtunes.setSetting('custom_path', library)
        LOG.info("TV Tunes custom path is enabled and set.")
    else:
        dialog("ok", heading="{emby}", line1=_(33152))

        return

    with Database('emby') as embydb:
        all_views = emby_db.EmbyDatabase(embydb.cursor).get_views()
        views = [x[0] for x in all_views if x[2] in ('movies', 'tvshows', 'mixed')]

    get_server()
    items = {}
    server = EMBY['auth/server-address']
    token = EMBY['auth/token']

    for view in views:
        result = EMBY['api'].get_items_theme_video(view)

        for item in result['Items']:

            folder = normalize_string(item['Name'].encode('utf-8'))
            items[item['Id']] = folder

        result = EMBY['api'].get_items_theme_song(view)

        for item in result['Items']:

            folder = normalize_string(item['Name'].encode('utf-8'))
            items[item['Id']] = folder

    for item in items:

        nfo_path = os.path.join(library, items[item]).decode('utf-8')
        nfo_file = os.path.join(nfo_path, "tvtunes.nfo").decode('utf-8')

        if not xbmcvfs.exists(nfo_path):
            xbmcvfs.mkdir(nfo_path)

        themes = EMBY['api'].get_themes(item)
        paths = []

        for theme in themes['ThemeVideosResult']['Items'] + themes['ThemeSongsResult']['Items']:
            putils = PlayUtils(theme, False, EMBY)

            if play:
                paths.append(putils.direct_play(theme['MediaSources'][0]))
            else:
                paths.append(putils.direct_url(theme['MediaSources'][0]))

        tvtunes_nfo(nfo_file, paths)

    dialog("notification", heading="{emby}", message=_(33153), icon="{emby}", time=1000, sound=False)

def delete_item():

    ''' Delete keymap action.
    '''
    import context

    context.Context(delete=True)

def backup():

    ''' Emby backup.
    '''
    from helper.utils import delete_folder, copytree

    path = settings('backupPath')
    folder_name = "Kodi%s.%s" % (xbmc.getInfoLabel('System.BuildVersion')[:2], xbmc.getInfoLabel('System.Date(dd-mm-yy)'))
    folder_name = dialog("input", heading=_(33089), defaultt=folder_name)

    if not folder_name:
        return

    backup = os.path.join(path, folder_name)

    if xbmcvfs.exists(backup + '/'):
        if not dialog("yesno", heading="{emby}", line1=_(33090)):

            return backup()

        delete_folder(backup)

    addon_data = xbmc.translatePath("special://profile/addon_data/plugin.video.emby").decode('utf-8')
    destination_data = os.path.join(backup, "addon_data", "plugin.video.emby")
    destination_databases = os.path.join(backup, "Database")

    if not xbmcvfs.mkdirs(path) or not xbmcvfs.mkdirs(destination_databases):

        LOG.info("Unable to create all directories")
        dialog("notification", heading="{emby}", icon="{emby}", message=_(33165), sound=False)

        return

    copytree(addon_data, destination_data)

    databases = objects.Objects().objects

    db = xbmc.translatePath(databases['emby']).decode('utf-8')
    xbmcvfs.copy(db, os.path.join(destination_databases, db.rsplit('\\', 1)[1]))
    LOG.info("copied emby.db")

    db = xbmc.translatePath(databases['video']).decode('utf-8')
    filename = db.rsplit('\\', 1)[1]
    xbmcvfs.copy(db, os.path.join(destination_databases, filename))
    LOG.info("copied %s", filename)

    if settings('enableMusic.bool'):

        db = xbmc.translatePath(databases['music']).decode('utf-8')
        filename = db.rsplit('\\', 1)[1]
        xbmcvfs.copy(db, os.path.join(destination_databases, filename))
        LOG.info("copied %s", filename)

    LOG.info("backup completed")
    dialog("ok", heading="{emby}", line1="%s %s" % (_(33091), backup))

def cache_artwork():

    from objects.kodi import artwork

    artwork.Artwork(None).cache_textures()
