def test_engine_modules_import():
    import terrariumArea, terrariumDatabase, terrariumEngine
    assert terrariumEngine.terrariumEngine and terrariumArea.terrariumArea

def test_sensor_db_is_real(sensor_db):
    from pony import orm
    from terrariumDatabase import Sensor
    with orm.db_session():
        Sensor(id="a", hardware="bme280", type="temperature", name="t", address="76", alarm_min=22, alarm_max=28)
    with orm.db_session():
        assert Sensor["a"].alarm_min == 22
