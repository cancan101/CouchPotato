from app.config.cplog import CPLog
from app.config.db import Session as Db, Movie, QualityTemplate, MovieQueue
from app.controllers import BaseController, url, redirect
from app.lib.xbmc import XBMC
from sqlalchemy.sql.expression import or_, desc
import cherrypy
import os
import sqlite3 as MySqlite

log = CPLog(__name__)

class MovieController(BaseController):

    @cherrypy.expose
    @cherrypy.tools.mako(filename = "movie/index.html")
    def index(self):
        '''
        Show all wanted, snatched, downloaded movies
        '''

        if cherrypy.request.path_info == '/':
            return redirect('movie/')

        qMovie = Db.query(Movie)
        movies = qMovie.order_by(Movie.name).filter(or_(Movie.status == u'want', Movie.status == u'waiting')).all()
        snatched = qMovie.order_by(desc(Movie.dateChanged), Movie.name).filter_by(status = u'snatched').all()
        downloaded = qMovie.order_by(desc(Movie.dateChanged), Movie.name).filter_by(status = u'downloaded').all()

        return self.render({'movies': movies, 'snatched':snatched, 'downloaded':downloaded})


    @cherrypy.expose
    def delete(self, id):
        '''
        Mark movie as deleted
        '''

        movie = Db.query(Movie).filter_by(id = id).one()
        previousStatus = movie.status

        #set status
        movie.status = u'deleted'
        Db.flush()

        if previousStatus == 'downloaded':
            self.flash.add('movie-' + str(movie.id), '"%s" deleted.' % (movie.name))
            return redirect(url(controller = 'movie', action = 'index'))

    @cherrypy.expose
    def downloaded(self, id):
        '''
        Mark movie as downloaded
        '''

        movie = Db.query(Movie).filter_by(id = id).one()

        #set status
        movie.status = u'downloaded'
        Db.flush()

        self.flash.add('movie-' + str(movie.id), '"%s" marked as downloaded.' % (movie.name))
        return redirect(url(controller = 'movie', action = 'index'))


    @cherrypy.expose
    def reAdd(self, id, **data):
        '''
        Re-add movie and force search
        '''

        movie = Db.query(Movie).filter_by(id = id).one()

        if data.get('failed'):
            for history in movie.history:
                if history.status == u'snatched':
                    history.status = u'ignore'
                    Db.flush()

        for x in movie.queue:
            if x.completed == True:
                x.completed = False
                Db.flush()
                break

        #set status
        movie.status = u'want'
        Db.flush()

        #gogo find nzb for added movie via Cron
        self.cron.get('yarr').forceCheck(id)
        self.searchers.get('etaQueue').put({'id':id})

        self.flash.add('movie-' + str(movie.id), '"%s" re-added.' % (movie.name))
        return redirect(url(controller = 'movie', action = 'index'))

    @cherrypy.expose
    @cherrypy.tools.mako(filename = "movie/add.html")
    def add(self):

        return self.render({})

    @cherrypy.expose
    @cherrypy.tools.mako(filename = "movie/search.html")
    def search(self, **data):
        '''
        Search for added movie. 
        Add if only 1 is found
        '''
        moviename = data.get('moviename')
        movienr = data.get('movienr')
        quality = data.get('quality')
        year = data.get('year')

        if data.get('add'):
            results = cherrypy.session.get('searchresults')
            if not results:
                log.error('Researching for results..')
                results = self.searchers.get('movie').find(moviename)
            result = results[int(movienr)]

            if year:
                result.year = year

            if result.year == 'None' or not result.year:
                return self.render({'error': 'year'})
            else:
                self._addMovie(result, quality)
                return redirect(cherrypy.request.headers.get('referer'))
        else:
            results = self.searchers.get('movie').find(moviename)
            cherrypy.session['searchresults'] = results

        return self.render({'moviename':moviename, 'results': results, 'quality':quality})

    @cherrypy.expose
    @cherrypy.tools.mako(filename = "movie/imdbAdd.html")
    def imdbAdd(self, **data):
        '''
        Add movie by imdbId
        '''

        id = 'tt' + data.get('id').replace('tt', '')
        success = False

        result = Db.query(Movie).filter_by(imdb = id, status = u'want').first()
        if result:
            success = True

        if data.get('add'):
            result = self.searchers.get('movie').findByImdbId(id)

            self._addMovie(result, data.get('quality'), data.get('year'))
            log.info('Added : %s' % result.name)
            success = True

        return self.render({'id':id, 'result':result, 'success':success, 'year':data.get('year')})

    @staticmethod
    def _generateSQLQuery(movie):
#        return "select c09 from movie where c09='%s'" % (movie.imdb)
        return "select c09, strVideoCodec, fVideoAspect, iVideoWidth, iVideoHeight from movie left outer join streamdetails on streamdetails.idFile = movie.idFile and iStreamType = 0 where c09='%s'" % (movie.imdb)
    
    @staticmethod
    def _classifyQuality(strVideoCodec, fVideoAspect, iVideoWidth, iVideoHeight):
        if fVideoAspect is None or strVideoCodec is None or iVideoWidth is None or iVideoHeight is None:
            return "Unknown" # this might be caused by iso files
        
        try:
            iVideoWidth = int(iVideoWidth)
            iVideoHeight = int(iVideoHeight)
        except ValueError:
            return "Unknown"
        
        if iVideoWidth >= 1900: # 1920 is exactly width of 1080
            return "1080p"
        elif iVideoHeight == 1080:
            return "1080p"
        elif iVideoWidth >= 1276:# 1280 is exactly width of 720
            return "720p"
        elif iVideoWidth == 720:
            return "720p"
        elif iVideoWidth in [720, 704, 352] or iVideoHeight in [480]: # DVD
            return "dvdrip"
        else:
            return "Unknown" # What about: 640x288 528x240 608x336 640x352 592x320 528x384 688x368 624x336 544x240 
        
    def _checkMovieExists(self, movie, qualityId):
        if cherrypy.config.get('config').get('XBMC', 'dbpath'):
            dbfile = None
            for root, dirs, files in os.walk(cherrypy.config.get('config').get('XBMC', 'dbpath')):
                for file in files:
                    if file.startswith('MyVideos'):
                        dbfile = os.path.join(root, file)

            if dbfile:
                #------Opening connection to XBMC DB------
                connXbmc = MySqlite.connect(dbfile)
                if connXbmc:
                    log.debug('Checking if movie exists in XBMC by IMDB id:' + movie.imdb)
                    connXbmc.row_factory = MySqlite.Row
                    cXbmc = connXbmc.cursor()
                    #sqlQuery = 'select c09 from movie where c09="' + movie.imdb + '"'
                    sqlQuery = self._generateSQLQuery(movie)
                    cXbmc.execute(sqlQuery)
                    #------End of Opening connection to XBMC DB------
                    inXBMC = False
                    for rowXbmc in cXbmc: # do a final check just to be sure
                        log.debug('Found in XBMC:' + rowXbmc["c09"])
                        if movie.imdb == rowXbmc["c09"]:
                            inXBMC = True
                        else:
                            inXBMC = False

                    cXbmc.close()

                    if inXBMC:
                        log.info('Movie already exists in XBMC, skipping.')
                        return True
                else:
                    log.info('Could not connect to the XBMC database at ' + cherrypy.config.get('config').get('XBMC', 'dbpath'))
            else:
                log.info('Could not find the XBMC MyVideos db at ' + cherrypy.config.get('config').get('XBMC', 'dbpath'))

        if cherrypy.config.get('config').get('XBMC', 'useWebAPIExistingCheck'):
            quality = Db.query(QualityTemplate).filter_by(id = qualityId).one()
            acceptableQualities = [x.type for x in filter(lambda x: x.markComplete, quality.types)]
            log.debug("acceptableQualities = %s" % acceptableQualities) # log.info(["%s %s %s %s %s" % (x.id, x.markComplete, x.order, x.quality, x.type) for x in sorted(quality.types, key=lambda x:x.order)])
            
            xbmc = XBMC()

            sqlQuery = self._generateSQLQuery(movie)
            xbmcResultsHosts = xbmc.queryVideoDatabase(sqlQuery)
            
            if xbmcResultsHosts:
                for xmbcResults in xbmcResultsHosts:
                    records = xmbcResults.strip().split("<record>")
                    for xmbcResult in records:
                        xmbcResult = xmbcResult.replace("</record>", "")
                        
                        if xmbcResult == "":
                            continue
                        
                        fields = [x.replace("<field>", "") for x in filter(lambda x: x.startswith("<field>"), xmbcResult.split("</field>"))]
                    
                        log.debug("fields = %s" % fields)
                        
                        if len(fields) < 5:
                            log.error("Invalid response: %s" % xmbcResult)
                            continue
                        
                        imdbId, strVideoCodec, fVideoAspect, iVideoWidth, iVideoHeight = fields[:5]
                        if imdbId==movie.imdb:
                            existingMovieQuality = self._classifyQuality(strVideoCodec, fVideoAspect, iVideoWidth, iVideoHeight)
                            
                            if existingMovieQuality == "Unknown":
                                log.info("Unable to classify movie: strVideoCodec=%s, fVideoAspect=%s, iVideoWidth=%s, iVideoHeight=%s" % (strVideoCodec, fVideoAspect, iVideoWidth, iVideoHeight))
                                
                            treatUnknownAs = cherrypy.config.get('config').get('XBMC', 'unknown')
                            if treatUnknownAs and treatUnknownAs != '':
                                existingMovieQuality = treatUnknownAs
                                
                            if existingMovieQuality in acceptableQualities:
                                log.info('Movie already exists in XBMC (web API call), with acceptable quality (%s), skipping.' % (existingMovieQuality))
                                return True
                            else:
                                log.info('Movie already exists in XBMC (web API call), but quality (%s) incorrect (require: %s).' % (existingMovieQuality, acceptableQualities))

        return False

    def _addMovie(self, movie, quality, year = None):

        if self._checkMovieExists(movie=movie, qualityId=quality):
            return

        log.info('Adding movie to database: %s' % movie.name)

        if movie.id:
            exists = Db.query(Movie).filter_by(movieDb = movie.id).first()
        else:
            exists = Db.query(Movie).filter_by(imdb = movie.imdb).first()

        if exists:
            log.info('Movie already exists, do update.')

            # Delete old qualities
            for x in exists.queue:
                x.active = False
                Db.flush()

            new = exists
        else:
            new = Movie()
            Db.add(new)

        # Update the stuff
        new.status = u'want'
        new.name = movie.name
        new.imdb = movie.imdb
        new.movieDb = movie.id
        new.quality = quality
        new.year = year if year and movie.year == 'None' else movie.year
        Db.flush()

        # Add qualities to the queue
        quality = Db.query(QualityTemplate).filter_by(id = quality).one()
        for type in quality.types:
            queue = MovieQueue()
            queue.qualityType = type.type
            queue.movieId = new.id
            queue.order = type.order
            queue.active = True
            queue.completed = False
            queue.waitFor = 0 if type.markComplete else quality.waitFor
            queue.markComplete = type.markComplete
            Db.add(queue)
            Db.flush()

        #Get xml from themoviedb and save to cache
        self.searchers.get('movie').getExtraInfo(new.id, overwrite = True)

        #gogo find nzb for added movie via Cron
        self.cron.get('yarr').forceCheck(new.id)

        self.flash.add('movie-' + str(new.id), '"%s" (%s) added.' % (new.name, new.year))

    @cherrypy.expose
    def clear_downloaded(self):
        """Clear downloaded movies."""
        qMovie = Db.query(Movie)
        downloaded = qMovie.filter_by(status = u'downloaded').all()
        for movie in downloaded:
          movie.status = u'deleted'
        Db.flush()
        return redirect(url(controller = 'movie', action = 'index'))
