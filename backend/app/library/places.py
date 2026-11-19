"""Cities offered as choices when someone picks target cities (owner redesign, plans 3.3 and 5.1).

The country or region only narrows the list of choices; it is never a condition of its own, and switching it leaves the
cities already chosen as they are. A city is saved as the name written here (Chinese names for the cities in China, as
the search reads them; the usual English names elsewhere); any other city can still be typed in full. The list is a
convenience for choosing, not a list of where jobs can be found.
"""

# (country key, the catalog key of its name, its cities)
CITY_CHOICES = (
    ("cn", "places.cn", ("北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "武汉", "西安", "天津", "重庆", "长沙", "厦门", "青岛")),
    ("hk", "places.hk", ("香港",)),
    ("sg", "places.sg", ("Singapore",)),
    ("us", "places.us", ("New York", "San Francisco", "Seattle", "Boston", "Los Angeles", "Chicago", "Austin")),
    ("ca", "places.ca", ("Toronto", "Vancouver", "Montreal")),
    ("uk", "places.uk", ("London",)),
)
