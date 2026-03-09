import random
from telegram import ReplyKeyboardRemove

def generate_question(flags_dict):
    flag, capitals = random.choice(list(flags_dict.items()))
    return flag, capitals, ReplyKeyboardRemove() 