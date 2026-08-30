from aiogram.fsm.state import State, StatesGroup


class ProfileForm(StatesGroup):
    consent = State()
    name = State()
    birth_date = State()
    time_precision = State()
    birth_time = State()
    city = State()
    city_choice = State()
    confirm = State()


class PaymentForm(StatesGroup):
    email = State()


class DeleteForm(StatesGroup):
    confirm = State()
