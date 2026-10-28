# 테스트용으로 직접 만든 가짜 게임 스크립트 (저작물 아님)
define a = Character(_("Alice"), color="#c8ffc8")
define b = Character("Bob")

default met_bob = False

label start:
    scene bg room
    with fade

    "It was a quiet morning."
    a "Good morning, [player_name]!"
    a happy "I made {b}coffee{/b} for you."
    voice "a_003.ogg"
    a "Do you want some?"

    menu:
        "Yes, please.":
            a "Here you go."
            $ met_bob = True
        "No, thanks." if not met_bob:
            a "Suit yourself."

    b "Hey, it's %(time)s already.\nWe're late!"
    "Bob" "Let's go."
    jump chapter2

label chapter2:
    "The end of the demo."
    a "Thanks for playing!" with dissolve
    return
