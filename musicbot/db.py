import sys
from sqlalchemy import create_engine, Table, Integer, String, Column, ForeignKey, Boolean, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.exc import ProgrammingError, OperationalError


def base_repr(self):
    """
    Monkeypatch the Base object so it's `eval`able

    :param self:
    :return str:
    """
    params = ", ".join("{}={}".format(column.key, repr(getattr(self, column.key)))
                       for column in self.__table__.columns)
    return "{}({})".format(self.__class__.__name__, params)


Base = declarative_base()
Base.__repr__ = base_repr

association_table = Table('association',
                          Base.metadata,
                          Column('permission_group_id', Integer, ForeignKey('PermissionsGroup.id')),
                          Column('user_id', Integer, ForeignKey('User.id')))


class Server(Base):
    __tablename__ = "Server"
    id = Column(Integer, primary_key=True)
    discord_id = Column(String)

    permission_groups = relationship("PermissionsGroup")

    regular_id = Column(String)
    fresh_id = Column(String)

    report_id = Column(String)
    debug_report_id = Column(String)
    warning_id = Column(String)
    survey_id = Column(String)
    autoconnect_id = Column(String)
    bind_to_channels = Column(String)

    volume = Column(Integer)
    max_skips = Column(Integer)
    ratio = Column(Float)
    autoplaylist = Column(String)

    command_prefix = Column(String)

    def setup(self, bot):
        self.bot = bot

    @property
    def server(self):
        return self.bot.get_server(self.server_id)

    @property
    def debug_report_channel(self):
        return self.bot.get_channel(self.debug_report_id)

    @property
    def warning_channel(self):
        return self.bot.get_channel(self.warning_id)

    @property
    def survey_channel(self):
        return self.bot.get_channel(self.survey_id)

    @property
    def autoconnect_channel(self):
        return self.bot.get_channel(self.autoconnect_id)

    @property
    def bound_channels(self):
        return [self.bot.get_channel(channel_id) for channel_id in self.bind_to_channels.split(" ")]


class PermissionsGroup(Base):
    __tablename__ = "PermissionsGroup"
    id = Column(Integer, primary_key=True)
    group_name = Column(String)
    command_whitelist = Column(String)
    command_voice_blacklist = Column(String)
    new_command_allow = Column(Boolean)

    max_songs = Column(Integer)
    max_song_length = Column(Integer)
    instaskip = Column(Boolean)

    users = relationship("User",
                         secondary=association_table,
                         back_populates="groups")
    roles = Column(String)

    server_id = Column(Integer, ForeignKey(Server.id, ondelete="CASCADE"))
    server = relationship(Server, foreign_keys=server_id)


class User(Base):
    __tablename__ = "User"
    id = Column(Integer, primary_key=True)
    discord_id = Column(Integer)
    refresh_token = Column(String)
    agreed = Column(Boolean)

    groups = relationship(PermissionsGroup,
                          secondary=association_table,
                          back_populates="users")


def init_db():
    engine = create_engine('sqlite:///musicbot.db')

    if "reset_db" in sys.argv:
        print("Resetting database...")
        for table in Base.metadata.tables.values():
            try:
                engine.execute("DROP TABLE {};".format(table))
            except ProgrammingError:
                try:
                    engine.execute('DROP TABLE "{}";'.format(table))
                except ProgrammingError:
                    pass
            except OperationalError:
                pass

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    return engine, session
