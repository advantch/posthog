import json
from uuid import uuid4

from ee.clickhouse.client import sync_execute
from ee.clickhouse.models.event import create_event
from ee.clickhouse.queries.util import format_ch_timestamp
from ee.clickhouse.util import ClickhouseTestMixin
from posthog.api.test.test_person import factory_test_person
from posthog.models import Event, Person


def _create_event(**kwargs):
    kwargs.update({"event_uuid": uuid4()})
    return Event(pk=create_event(**kwargs))


def _get_events():
    return sync_execute("SELECT * FROM events")


def _create_person(**kwargs):
    postgres_person: Person = Person.objects.create(**kwargs)
    sync_execute(
        """
        INSERT INTO person (id, created_at, team_id, properties, is_identified)
        VALUES (%(id)s, %(created_at)s, %(team_id)s, %(properties)s, %(is_identified)s)""",
        {
            "id": postgres_person.uuid,
            "created_at": postgres_person.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "team_id": postgres_person.team_id,
            "properties": json.dumps(postgres_person.properties),
            "is_identified": int(postgres_person.is_identified),
        },
    )
    return postgres_person


class ClickhouseTestPersonApi(
    ClickhouseTestMixin, factory_test_person(_create_event, _create_person, _get_events, Person.objects.all)  # type: ignore
):
    def test_filter_id_or_uuid(self) -> None:
        # Overriding this test due to only UUID being available on ClickHouse
        person1 = _create_person(team=self.team, properties={"$browser": "whatever", "$os": "Mac OS X"})
        person2 = _create_person(team=self.team, properties={"random_prop": "asdf"})
        _create_person(team=self.team, properties={"random_prop": "asdf"})

        response_uuid = self.client.get("/api/person/?uuid={},{}".format(person1.uuid, person2.uuid))
        self.assertEqual(response_uuid.status_code, 200)
        self.assertEqual(len(response_uuid.json()["results"]), 2)

        response_id = self.client.get("/api/person/?id={},{}".format(person1.id, person2.id))
        self.assertEqual(response_id.status_code, 422)
