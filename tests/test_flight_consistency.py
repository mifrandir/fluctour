import unittest

from fluctour.planning import validate_result
from test_fluctour import dossier, option


def leg(identifier, origin, destination, departure=None, arrival=None, cabin='economy'):
    return dict(id=identifier, origin=origin, destination=destination, departure=departure,
                arrival=arrival, carrier='Fixture', cabin=cabin, paid_by='personal')


class FlightConsistencyTests(unittest.TestCase):
    def test_contradictory_trip_envelope_and_legs_reject(self):
        route = [leg('out','HKG','LHR','2027-06-01T20:00:00+08:00','2027-06-02T06:00:00+01:00'),
                 leg('back','LHR','HKG','2027-06-20T20:00:00+01:00','2027-06-21T15:00:00+08:00')]
        with self.assertRaisesRegex(ValueError, 'boundary'):
            validate_result(dossier([option(legs=route)]))

    def test_disconnected_airports_require_an_explicit_transfer(self):
        route = [leg('out','HKG','LHR'),leg('back','LGW','HKG')]
        with self.assertRaisesRegex(ValueError, 'disconnected'):
            validate_result(dossier([option(legs=route)]))
        route.insert(1,leg('ground','LHR','LGW',cabin='ground'))
        validate_result(dossier([option(legs=route)]),home_airport='HKG')

    def test_known_home_airport_is_checked_without_inference(self):
        route=[leg('out','HKG','LHR'),leg('back','LHR','NRT')]
        with self.assertRaisesRegex(ValueError, 'home airport'):
            validate_result(dossier([option(legs=route)]),home_airport='HKG')

    def test_equivalent_utc_offsets_match_and_unknowns_remain_unknown(self):
        route=[leg('out','HKG','NRT','2027-01-07T14:30:00Z',None),
               leg('back','NRT','HKG',None,'2027-01-10T11:00:00Z')]
        validate_result(dossier([option(legs=route)]),home_airport='HKG')
        route[0]['departure']=None
        route[1]['arrival']=None
        validate_result(dossier([option(legs=route)]),home_airport='HKG')

    def test_intermediate_leg_outside_the_envelope_rejects(self):
        route=[leg('out','HKG','NRT'),leg('side','NRT','KIX','2027-02-01T10:00:00+09:00',None),
               leg('back','KIX','HKG')]
        with self.assertRaisesRegex(ValueError, 'within the trip'):
            validate_result(dossier([option(legs=route)]),home_airport='HKG')
